import type { Config } from "tailwindcss";
export default { content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"], theme: { extend: { colors: { ink: "#12213a", teal: "#0f9f8f" } } }, plugins: [] } satisfies Config;
