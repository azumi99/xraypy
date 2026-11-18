/** @type {import('tailwindcss').Config} */
// Design tokens from the cv-service design system (UI/UX Pro Max):
// dark tech + status green, glassmorphism surfaces, DM Sans.
module.exports = {
  content: ["./static/index.html"],
  theme: {
    extend: {
      colors: {
        ink: "#F8FAFC",
        muted: "#94A3B8",
        surface: {
          DEFAULT: "#0F172A",
          card: "#1B2336",
        },
        line: "rgba(71, 85, 105, 0.4)",
        brand: {
          DEFAULT: "#3B82F6",
          deep: "#2563EB",
          soft: "#93C5FD",
        },
        ok: "#22C55E",
        warn: "#F97316",
        bad: "#EF4444",
        hold: "#EAB308",
      },
      fontFamily: {
        sans: ["DM Sans", "sans-serif"],
      },
      boxShadow: {
        glow: "0 8px 30px rgba(59, 130, 246, 0.3)",
        "glow-red": "0 8px 24px rgba(239, 68, 68, 0.3)",
        "glow-purple": "0 8px 30px rgba(168, 85, 247, 0.3)",
      },
      backgroundImage: {
        "grad-nav": "linear-gradient(90deg, #2563EB, #3B82F6)",
        "grad-brand": "linear-gradient(135deg, #2563EB, #A855F7)",
      },
    },
  },
  plugins: [],
};
