"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { CostBadge } from "@/components/CostBadge";
import { cn } from "@/lib/utils";

/**
 * Global chrome. Rendered on every route except `/first-run`, where the user
 * has not yet provided an API key and should see nothing but the setup wall
 * (spec §7.7.1).
 *
 * The bar is deliberately spare: two nav items, the cost badge, a hairline
 * rule below. Typography: sans, small, tracking-tight. No logo, no avatar,
 * nothing that suggests "marketing page."
 */

const NAV: Array<{ href: string; label: string; matchPrefix?: string }> = [
  { href: "/", label: "Study" },
  { href: "/upload", label: "Upload", matchPrefix: "/upload" },
  { href: "/search", label: "Search", matchPrefix: "/search" },
  { href: "/settings/api-keys", label: "Settings", matchPrefix: "/settings" },
];

export function TopBar() {
  const pathname = usePathname() ?? "";

  if (pathname.startsWith("/first-run")) return null;

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur-none">
      <div className="mx-auto flex h-12 w-full max-w-6xl items-center gap-6 px-6">
        <Link
          href="/"
          className="font-serif text-sm font-semibold tracking-tight text-foreground"
        >
          cLAWd<span className="text-muted-foreground font-normal"> — study system</span>
        </Link>
        <nav className="flex items-center gap-5 text-sm">
          {NAV.map((item) => {
            const isActive = item.matchPrefix
              ? pathname.startsWith(item.matchPrefix)
              : pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "border-b-2 border-transparent py-3 text-sm tracking-tight transition-colors",
                  isActive
                    ? "border-accent text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <CostBadge />
        </div>
      </div>
    </header>
  );
}

export default TopBar;
