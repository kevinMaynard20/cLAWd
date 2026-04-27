import ClientPage from "./ClientPage";

// Pre-render only a placeholder shell at build time; real artifact ids are
// runtime values. `dynamicParams = true` is what lets Next's client router
// re-use the shell for any param at runtime — without it, an unknown param
// 404s and Tauri's WebView falls back to /index.html (the dashboard), which
// looks like "the page snaps back home" from the user's perspective.
export function generateStaticParams() {
  return [{ artifactId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ artifactId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
