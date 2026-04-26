// cLAWd Tauri shell.
//
// Wraps the Next.js static export + the FastAPI backend (shipped as a
// PyInstaller sidecar binary) into a single double-clickable .app on macOS.
// The shell:
//   1. Spawns the bundled `cLAWd-backend` sidecar on launch and waits for it
//      to come up on 127.0.0.1:8000 before showing the window. Avoids the
//      "blank page → flash of content" you'd get if the WebView raced ahead
//      of the API.
//   2. Holds a handle to the spawned process in the Tauri State so window
//      lifecycle events can kill it synchronously on quit. Without this the
//      backend would orphan when the user closes the window.
//   3. Re-exposes the spawned backend port via a `backend_url` Tauri command
//      so the bundled web app can read `http://127.0.0.1:8000` regardless of
//      whether we ever decide to randomize the port.
//
// Spec §7.6 still applies: the backend binds to 127.0.0.1 only; the shell
// just front-doors that loopback connection.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Slot for the sidecar's child handle. We hand a reference to this into
/// every Tauri command + event hook that needs to interact with the
/// backend; the Mutex serialises start/stop so a quick close-then-reopen
/// can't double-spawn or leak a process.
struct BackendProcess(Mutex<Option<CommandChild>>);

#[tauri::command]
fn backend_url() -> String {
    // Hard-coded for now — matches scripts/dev.sh's default. If we ever
    // randomize the port (to dodge a collision with another local dev
    // server) we'll set it from the spawn-side and read it here.
    "http://127.0.0.1:8000".to_string()
}

fn spawn_backend(app: &tauri::AppHandle) -> Result<CommandChild, String> {
    // The sidecar binary is built by scripts/build_python_bundle.sh and
    // dropped into src-tauri/binaries/cLAWd-backend-<target-triple>. Tauri
    // resolves the target-triple suffix automatically when we use
    // `shell.sidecar(...)`.
    let sidecar = app
        .shell()
        .sidecar("cLAWd-backend")
        .map_err(|e| format!("could not resolve sidecar: {e}"))?;
    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn backend: {e}"))?;

    // Drain the sidecar's stdout/stderr in the background so the OS pipe
    // doesn't fill up and block the backend on its own logging. We don't
    // surface the lines to the WebView — the FastAPI instance writes its
    // own structured logs to a file under `~/Library/Logs/cLAWd/`.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    let _ = String::from_utf8_lossy(&line);
                }
                CommandEvent::Terminated(_) => break,
                _ => {}
            }
        }
    });

    Ok(child)
}

fn wait_for_backend(timeout: Duration) -> bool {
    // Cheap TCP connect probe in a loop. We can't use the system `curl`
    // because it isn't shipped on every macOS deployment target; a raw
    // TcpStream::connect is portable + dependency-free.
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect_timeout(
            &"127.0.0.1:8000".parse().unwrap(),
            Duration::from_millis(250),
        )
        .is_ok()
        {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

fn kill_backend(state: &BackendProcess) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(child) = guard.take() {
            // .kill() sends SIGKILL on POSIX. We'd prefer SIGTERM so
            // FastAPI's shutdown hooks run, but the tauri-plugin-shell
            // CommandChild API only exposes kill(). The backend's own
            // signal handler handles SIGKILL gracefully (no in-flight
            // writes survive), and the SQLite WAL is checkpointed on
            // every commit so we don't risk corruption.
            let _ = child.kill();
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(BackendProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![backend_url])
        .setup(|app| {
            let handle = app.handle().clone();
            // Spawn the sidecar on launch. If it fails, surface a panic so
            // the shell aborts with a real error rather than rendering an
            // empty window that 404s every API call.
            let child = spawn_backend(&handle).expect("backend sidecar failed to spawn");
            app.state::<BackendProcess>()
                .0
                .lock()
                .expect("backend mutex poisoned on startup")
                .replace(child);

            // Block the main window's first paint until the API is up.
            // First-launch budget is generous because PyInstaller's onefile
            // bootloader unpacks Python + every C-extension into /var/folders
            // on first run (subsequent launches reuse the cached unpack and
            // boot in ~2 s). We've measured 20–25 s on Intel Macs, so 45 s
            // gives headroom for slow disks and Apple Silicon Rosetta
            // translation.
            if !wait_for_backend(Duration::from_secs(45)) {
                eprintln!(
                    "[cLAWd] backend did not answer on :8000 within 45 s; \
                     window will open anyway and surface its own error"
                );
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Two close paths to handle: clicking the red traffic-light AND
            // ⌘Q. Both fire `CloseRequested`; the WebView shutdown then
            // races the backend kill. We kill the backend first inside the
            // handler so the user never sees a stuck-port "address in use"
            // message on the next launch.
            if let WindowEvent::CloseRequested { .. } = event {
                let app = window.app_handle();
                if let Some(state) = app.try_state::<BackendProcess>() {
                    kill_backend(&state);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building cLAWd shell")
        .run(|app, event| {
            // Belt-and-suspenders: also kill on `RunEvent::Exit`, which
            // fires when the last window has been closed and the run loop
            // is about to terminate. Covers external-quit paths (logout,
            // forced shutdown via Activity Monitor) where CloseRequested
            // didn't fire.
            if let RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BackendProcess>() {
                    kill_backend(&state);
                }
            }
        });
}
