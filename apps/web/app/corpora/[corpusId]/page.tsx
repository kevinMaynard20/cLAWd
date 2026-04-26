import ClientPage from "./ClientPage";

// Static export shim. With Next 15's `output: "export"` (Tauri build), the
// build needs a concrete list of params to pre-render. Real corpus ids are
// generated at runtime in the local SQLite DB, so we emit a single
// `__shell__` placeholder. The Tauri WebView always enters via the
// dashboard and follows <Link> clicks, so the client router handles every
// real URL — the placeholder HTML is never visited directly.
export function generateStaticParams() {
  return [{ corpusId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ corpusId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
