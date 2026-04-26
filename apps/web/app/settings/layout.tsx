import Link from "next/link";

/**
 * Settings shell. The left rail lists the three sub-pages (API keys, Costs,
 * Models — the Models page is scaffolded empty in Phase 1). A serif heading
 * sits at the top of the column rail so the page echoes the typographic
 * distinction between chrome (sans) and subject matter.
 */

const NAV = [
  { href: "/settings/api-keys", label: "API keys" },
  { href: "/settings/costs", label: "Costs" },
  { href: "/settings/models", label: "Models" },
];

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <main className="mx-auto w-full max-w-6xl px-6 py-10">
      <div className="grid grid-cols-1 gap-10 md:grid-cols-[200px_1fr]">
        <aside>
          <p className="font-serif text-xs uppercase tracking-[0.18em] text-muted-foreground">
            Settings
          </p>
          <nav className="mt-3 flex flex-col gap-1 border-l border-border pl-3">
            {NAV.map((item) => (
              <SettingsNavLink key={item.href} href={item.href}>
                {item.label}
              </SettingsNavLink>
            ))}
          </nav>
        </aside>
        <section>{children}</section>
      </div>
    </main>
  );
}

function SettingsNavLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className="border-l-2 border-transparent -ml-3 pl-3 py-1.5 text-sm tracking-tight text-muted-foreground transition-colors hover:text-foreground aria-[current=page]:border-accent aria-[current=page]:text-foreground"
    >
      {children}
    </Link>
  );
}
