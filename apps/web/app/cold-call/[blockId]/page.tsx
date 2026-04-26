import ClientPage from "./ClientPage";

export function generateStaticParams() {
  return [{ blockId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ blockId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
