"use client";

import Link from "next/link";

/**
 * In-app help page for getting an Anthropic API key. Embeds a YouTube
 * walkthrough plus a written checklist so the page is useful even with
 * audio off / network throttled.
 *
 * Why a Next page and not a plain external link: in the Tauri-bundled
 * shell, anchor tags with `target="_blank"` are silently swallowed by the
 * WebView (no system-browser handoff without the shell plugin). Rendering
 * the video inline keeps the tutorial accessible inside the app.
 */

const VIDEO_ID = "vgncj7MJbVU";

export default function ApiKeyHelpPage() {
  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-10">
      <Link
        href="/first-run"
        className="text-xs uppercase tracking-[0.18em] text-muted-foreground hover:text-foreground"
      >
        ← Back to setup
      </Link>
      <h1 className="mt-2 font-serif text-3xl font-semibold tracking-tight">
        How to get your Anthropic API key
      </h1>
      <p className="mt-2 max-w-prose text-sm text-muted-foreground">
        cLAWd uses your own Anthropic API key for every Claude call. The key
        is stored in macOS Keychain — never written to a file inside the app.
        You only do this once.
      </p>

      <div
        className="mt-6 overflow-hidden rounded-sm border border-border bg-black"
        style={{ aspectRatio: "16 / 9" }}
      >
        <iframe
          src={`https://www.youtube.com/embed/${VIDEO_ID}?rel=0`}
          title="How to get an Anthropic API key"
          className="h-full w-full"
          frameBorder={0}
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
          referrerPolicy="strict-origin-when-cross-origin"
          allowFullScreen
        />
      </div>

      <section className="mt-8 border-t border-border pt-6">
        <h2 className="font-serif text-xl font-semibold tracking-tight">
          Quick steps
        </h2>
        <ol className="mt-3 space-y-3 font-serif text-sm leading-relaxed">
          <li>
            <strong>1.</strong> Go to{" "}
            <code className="rounded-sm bg-muted px-1 py-0.5 font-mono text-xs">
              console.anthropic.com
            </code>{" "}
            and sign in (or sign up — accounts are free).
          </li>
          <li>
            <strong>2.</strong> Add a payment method. Anthropic doesn&apos;t
            offer a free tier; you&apos;ll pay per call. Briefing one case
            costs roughly $0.05; a full Socratic drill session, a few
            cents per turn. cLAWd&apos;s built-in budget tracker lets you cap
            monthly spend.
          </li>
          <li>
            <strong>3.</strong> Open the <em>API Keys</em> tab in the left
            sidebar.
          </li>
          <li>
            <strong>4.</strong> Click <em>Create Key</em>. Pick any name
            (e.g. &ldquo;cLAWd&rdquo;). Copy the key — it starts with{" "}
            <code className="rounded-sm bg-muted px-1 py-0.5 font-mono text-xs">
              sk-ant-
            </code>
            . You&apos;ll only see the full key once.
          </li>
          <li>
            <strong>5.</strong> Paste it into the cLAWd setup screen.
            That&apos;s it.
          </li>
        </ol>
        <p className="mt-6 text-xs text-muted-foreground">
          The video above walks through the same steps if you&apos;d rather
          watch than read.
        </p>
      </section>

      <div className="mt-10 border-t border-border pt-6">
        <Link
          href="/first-run"
          className="text-xs uppercase tracking-[0.12em] text-accent hover:underline"
        >
          ← Back to setup
        </Link>
      </div>
    </main>
  );
}
