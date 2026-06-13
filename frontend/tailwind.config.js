/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          900: "#08090b",
          800: "#0c0e11",
          700: "#121519",
          600: "#171b20",
          500: "#1e242b",
          line: "#272d35",
        },
        signal: {
          DEFAULT: "#c9f24e",   // signal lime — primary accent
          dim: "#9bbf3a",
          glow: "rgba(201,242,78,0.18)",
        },
        amber: { DEFAULT: "#f5a524", dim: "#b9791a" },
        ice: { DEFAULT: "#56c8e6" },
        fog: {
          50: "#eef1f4",
          100: "#cdd3da",
          200: "#9aa4af",
          300: "#6c7681",
          400: "#4a525b",
        },
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', "system-ui", "sans-serif"],
        sans: ['"Hanken Grotesk"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      borderRadius: {
        DEFAULT: "3px",
        md: "4px",
        lg: "6px",
      },
      boxShadow: {
        panel: "0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 30px rgba(0,0,0,0.45)",
        glow: "0 0 0 1px rgba(201,242,78,0.4), 0 0 24px rgba(201,242,78,0.25)",
      },
      keyframes: {
        pulseDot: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
        sweep: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        riseIn: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseDot: "pulseDot 1.6s ease-in-out infinite",
        sweep: "sweep 1.8s ease-in-out infinite",
        riseIn: "riseIn 0.5s cubic-bezier(0.2,0.7,0.2,1) both",
      },
    },
  },
  plugins: [],
};
