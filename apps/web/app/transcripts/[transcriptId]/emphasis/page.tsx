import ClientPage from "./ClientPage";

export function generateStaticParams() {
  return [{ transcriptId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ transcriptId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
