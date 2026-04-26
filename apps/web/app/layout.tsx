import type { Metadata } from "next";
import { Inter, Source_Serif_4 } from "next/font/google";

import FirstRunGate from "@/components/FirstRunGate";
import TopBar from "@/components/TopBar";

import "./globals.css";

// Typography: sans for UI chrome, serif for case text and law-review-style
// section headings. The serif defaults to Source Serif 4 — the same family
// several U.S. law reviews adopted in the 2010s — with Georgia as a fallback.
const sans = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});

const serif = Source_Serif_4({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-serif",
});

export const metadata: Metadata = {
  title: "cLAWd — Law School Study System",
  description:
    "Local-first study system for 1L doctrinal courses. Briefs, Socratic drills, IRAC grading, transcript-to-emphasis mapping.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`h-full ${sans.variable} ${serif.variable}`}>
      <body
        className="min-h-full bg-background font-sans text-foreground antialiased"
        suppressHydrationWarning
      >
        <FirstRunGate>
          <TopBar />
          <div className="min-h-[calc(100vh-3rem)]">{children}</div>
        </FirstRunGate>
      </body>
    </html>
  );
}
