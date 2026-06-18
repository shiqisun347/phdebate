import { defineConfig } from "vitest/config";

// 纯函数 reducer 测试：node 环境即可，无需 jsdom/浏览器。
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
