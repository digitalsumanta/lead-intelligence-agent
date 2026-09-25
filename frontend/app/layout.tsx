import "./globals.css";
export const metadata = { title: "Lead Intelligence", description: "Local-first lead intelligence dashboard" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
