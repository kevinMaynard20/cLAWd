// Tauri build hook — compiles tauri.conf.json + asset paths into the binary
// at compile time. Required by `tauri-build`.
fn main() {
    tauri_build::build()
}
