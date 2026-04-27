import ClientPage from "./ClientPage";

// Static export shim. With Next 15's `output: "export"` (Tauri build), the
// build needs a concrete list of params to pre-render. Real corpus ids are
// generated at runtime in the local SQLite DB, so we emit a single
// `__shell__` placeholder. `dynamicParams = true` is what lets Next's
// client router render the shell for any param at runtime — without it,
// any unknown id 404s, and Tauri's WebView serves /index.html as the
// fallback (visible to the user as "the page snapped back to dashboard").
export function generateStaticParams() {
  return [{ corpusId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ corpusId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
