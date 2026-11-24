import "./globals.css";
import AuthBar from "@/components/AuthBar";

export const metadata = { title: "Lead Intelligence", description: "Local-first lead intelligence dashboard" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AuthBar />
        {children}
      </body>
    </html>
  );
}
