// Flat config (ESLint 9). Deliberately small: `tsc --noEmit` is the real type gate, and this
// catches the JavaScript-level mistakes it cannot — unused bindings, accidental globals,
// `==` where the operands are of different types.
import js from "@eslint/js";
import tsParser from "@typescript-eslint/parser";

export default [
  { ignores: ["dist/**", "node_modules/**", "*.config.ts", "src/**/*.d.ts"] },
  js.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaVersion: 2022, sourceType: "module", ecmaFeatures: { jsx: true } },
      globals: {
        window: "readonly",
        document: "readonly",
        fetch: "readonly",
        FormData: "readonly",
        File: "readonly",
        Blob: "readonly",
        URL: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        console: "readonly",
        HTMLInputElement: "readonly",
        HTMLDivElement: "readonly",
        HTMLTextAreaElement: "readonly",
        RequestInit: "readonly",
        ImportMeta: "readonly",
      },
    },
    rules: {
      // Both are TypeScript's job here, and its versions understand JSX, type-only imports
      // and constructor parameter properties — which the JavaScript rules read as unused.
      // `strict` + `noUnusedLocals` + `noUnusedParameters` are on in tsconfig.json.
      "no-undef": "off",
      "no-unused-vars": "off",
      eqeqeq: ["error", "smart"],
    },
  },
];
