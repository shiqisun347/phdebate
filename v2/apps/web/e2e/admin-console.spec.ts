import { expect, test } from "@playwright/test";

const account = process.env.E2E_ADMIN_ACCOUNT;
const password = process.env.E2E_ADMIN_PASSWORD;
test.skip(!account || !password, "需要一次性测试管理员凭据");

const sections = [
  ["系统总览", "系统总览"],
  ["比赛监管", "比赛监管"],
  ["用户管理", "用户管理"],
  ["赛事与题库", "赛事与题库"],
  ["自动流程", "自动比赛流程"],
  ["AI 与语音", "AI 辩手与语音服务"],
  ["媒体存储", "媒体存储与一致性"],
  ["结果复核", "比赛结果复核"],
  ["审计日志", "管理员审计日志"],
] as const;

test("系统管理员可在桌面与手机端访问全部管理栏目", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  await page.goto("login?next=/admin", { waitUntil: "networkidle" });
  await page.getByLabel("登录账号").fill(account!);
  await page.getByLabel("密码").fill(password!);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/admin$/, { timeout: 10_000 });
  await expect(page.getByRole("heading", { name: "稷下辩论系统管理" })).toBeVisible();
  await expect(page.getByText(/V2 已就绪|V2 需要检查/)).toBeVisible();

  for (const [tab, heading] of sections) {
    await page.locator(".admin-nav").getByRole("button", { name: tab, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, exact: true }).first()).toBeVisible();
  }

  await page.locator(".admin-nav").getByRole("button", { name: "系统总览", exact: true }).click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "管理后台不应产生整页横向滚动").toBeLessThanOrEqual(1);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("admin-overview.png"), fullPage: true });
  expect(errors).toEqual([]);
});
