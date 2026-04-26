import ClientPage from "./ClientPage";

export function generateStaticParams() {
  // Static-export sentinel. See /corpora/[corpusId]/page.tsx for the
  // rationale — the Tauri WebView enters via the dashboard and follows
  // Link components, so this placeholder is never user-visible.
  return [{ corpusId: "__shell__", bookId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ corpusId: string; bookId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
