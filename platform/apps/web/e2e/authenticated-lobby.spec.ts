import { expect, test, type BrowserContext, type Page } from "@playwright/test";

test.skip(process.env.E2E_MUTATING !== "true", "仅在一次性测试数据库中运行会写入数据的大厅流程");

async function register(context: BrowserContext, account: string, realName: string): Promise<Page> {
  const page = await context.newPage();
  await page.goto("register", { waitUntil: "networkidle" });
  await page.getByLabel("登录账号").fill(account);
  await page.getByLabel("真实姓名").fill(realName);
  await page.getByLabel("密码", { exact: true }).fill("Browser-test-1234");
  await page.getByLabel("确认密码").fill("Browser-test-1234");
  await page.getByRole("button", { name: "注册并进入赛场" }).click();
  await expect(page).toHaveURL(/\/$/, { timeout: 10_000 });
  await expect(page.getByText(realName, { exact: true })).toBeVisible();
  return page;
}

function trainingCard(page: Page) {
  return page.locator("article").filter({ hasText: "1v1 辩论训练赛" });
}

test("两名真实登录用户可创建、搜索、认领、准备并关闭同一房间", async ({ browser }, testInfo) => {
  const suffix = `${Date.now()}`.slice(-9);
  const contextOptions = {
    viewport: testInfo.project.use.viewport,
    isMobile: testInfo.project.use.isMobile,
    hasTouch: testInfo.project.use.hasTouch,
  };
  const ownerContext = await browser.newContext(contextOptions);
  const guestContext = await browser.newContext(contextOptions);
  const errors: string[] = [];
  try {
    const owner = await register(ownerContext, `owner_${suffix}`, "本地房主");
    const guest = await register(guestContext, `guest_${suffix}`, "本地队友");
    for (const page of [owner, guest]) {
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
    }

    await trainingCard(owner).getByRole("button", { name: "立即参赛" }).click();
    const createDialog = owner.getByRole("dialog", { name: /1v1 辩论训练赛/ });
    await createDialog.getByLabel("自定义辩题（留空使用题库）").fill("多人浏览器是否能可靠返回同一场辩论？");
    await createDialog.locator("form").getByRole("button", { name: "创建房间", exact: true }).click();
    await expect(owner).toHaveURL(/\/rooms\/(\d{6})\/lobby$/, { timeout: 10_000 });
    const roomCode = owner.url().match(/\/rooms\/(\d{6})\//)?.[1];
    expect(roomCode).toMatch(/^\d{6}$/);

    await trainingCard(guest).getByRole("button", { name: "立即参赛" }).click();
    const joinDialog = guest.getByRole("dialog", { name: /1v1 辩论训练赛/ });
    await joinDialog.getByRole("button", { name: /搜索房间/ }).click();
    await joinDialog.getByLabel("六位房间号").fill(roomCode!);
    await joinDialog.getByRole("button", { name: "进入房间" }).click();
    await expect(guest).toHaveURL(new RegExp(`/rooms/${roomCode}/lobby$`), { timeout: 10_000 });

    await guest.getByRole("button", { name: /反方1辩.*等待认领/ }).click();
    await expect(guest.getByRole("button", { name: /反方1辩.*未准备/ })).toBeDisabled();
    await guest.getByRole("button", { name: "确认准备" }).click();
    await expect(guest.getByRole("button", { name: "取消准备" })).toBeVisible();

    await owner.reload({ waitUntil: "networkidle" });
    await expect(owner.getByText("本地队友", { exact: true })).toBeVisible();
    await owner.getByRole("button", { name: "确认准备" }).click();
    await expect(owner.getByRole("button", { name: "锁定席位并开始" })).toBeEnabled();
    await owner.evaluate(() => window.scrollTo(0, 0));
    const overflow = await owner.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, "房间大厅不应产生整页横向滚动").toBeLessThanOrEqual(1);
    await owner.screenshot({ path: testInfo.outputPath("two-user-lobby.png"), fullPage: true });

    await guest.getByRole("button", { name: "释放当前席位" }).click();
    await expect(guest.getByText("认领一个空席后才能参赛")).toBeVisible();
    owner.once("dialog", (dialog) => dialog.accept());
    await owner.getByRole("button", { name: "关闭房间" }).click();
    await expect(owner).toHaveURL(/\/$/, { timeout: 10_000 });
    expect(errors).toEqual([]);
  } finally {
    await ownerContext.close();
    await guestContext.close();
  }
});
