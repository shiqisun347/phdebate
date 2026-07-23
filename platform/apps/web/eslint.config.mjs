import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // These React 19 compiler-oriented rules require architectural changes
      // to the verified WebSocket, device-control and realtime audio effects.
      // Keep the conventional hooks/dependency checks enabled, but do not
      // make a lint migration silently rewrite production synchronization.
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/refs": "off",
    },
  },
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "coverage/**",
    "test-results/**",
    "next-env.d.ts",
  ]),
]);
