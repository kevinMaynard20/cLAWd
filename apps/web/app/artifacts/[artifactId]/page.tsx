import ClientPage from "./ClientPage";

export function generateStaticParams() {
  return [{ artifactId: "__shell__" }];
}
export const dynamicParams = false;

export default function Page(props: {
  params: Promise<{ artifactId: string }>;
}) {
  return <ClientPage params={props.params} />;
}
