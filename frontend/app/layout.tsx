import { GeistMono } from "geist/font/mono"
import { GeistSans } from "geist/font/sans"
import type { Metadata, Viewport } from "next"
import type React from "react"

import { AppShell } from "@/components/app-shell"

import "./globals.css"

export const metadata: Metadata = {
  title: {
    default: "Wizard",
    template: "%s · Wizard",
  },
  description:
    "Ask a question, watch it think. Wizard plans the analysis, writes the Python, runs it in a sandbox and explains the result — on your machine, with your models.",
  // Only the icon that actually exists in /public is declared; the previous
  // manifest pointed at four files that were never added, so every page load
  // issued 404s for them.
  icons: { icon: "/favicon.ico" },
}

export const viewport: Viewport = {
  // Light only: one committed look, so the browser chrome is told exactly one
  // colour rather than being handed a scheme it can second-guess.
  themeColor: "#fdfcfb",
  colorScheme: "light",
  width: "device-width",
  initialScale: 1,
  // maximumScale/userScalable were pinned, which blocks pinch-zoom and fails
  // WCAG 1.4.4. Users need to be able to zoom a data table.
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    /*
      Geist ships its own woff2 files inside the npm package, so the type is
      self-hosted and `next build` never reaches the network for it. That
      matters: `pnpm build` is a CI gate, and `next/font/google` would have
      made it fail whenever Google Fonts was unreachable.
    */
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      {/*
        Extensions edit <body> before React hydrates -- Grammarly adds
        `data-gr-ext-installed` and `data-new-gr-c-s-check-loaded`, password
        managers and dark-mode add-ons do the same -- and the server HTML
        cannot know about them, so hydration reports a mismatch on every load
        for a difference the app did not cause and cannot prevent.

        `suppressHydrationWarning` applies to *this element only*, one level
        deep: attribute and text differences on <body> itself are ignored, and
        a real mismatch anywhere inside it still reports normally. That is what
        makes this safe rather than a blanket silencer.
      */}
      <body className="font-sans antialiased" suppressHydrationWarning>
        {/* Ambient wash. Fixed and inert, behind every route. */}
        <div className="aurora" aria-hidden="true" />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  )
}
