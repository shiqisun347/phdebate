import { expect, test, type Page, type TestInfo } from "@playwright/test";

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "页面不应出现横向滚动").toBeLessThanOrEqual(1);
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
}

function watchRuntimeErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  return errors;
}

test("赛事大厅、导航和参赛入口在桌面与手机端可用", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const runtimeErrors = watchRuntimeErrors(page);
  // The lobby intentionally polls live-room status, so networkidle is not a
  // valid readiness signal. Wait for the product UI that the test uses.
  await page.goto("./", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("heading", { name: /4v4 人机辩论正式赛.*参赛者自主组局/ })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("heading", { name: "选择赛事" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "4v4 人机辩论正式赛", exact: true })).toBeVisible();
  await expect(page.getByText("1v1 辩论训练赛", { exact: true })).toBeVisible();
  await expect(page.getByText(/场可观战/).first()).toBeVisible();
  await expect(page.getByText(/场进行中/)).toHaveCount(0);
  await expect(page.getByRole("link", { name: "登录" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "home");

  await page.getByRole("link", { name: "查看规则" }).first().click();
  await expect(page).toHaveURL(/\/competitions\/daily-4v4$/);
  expect(await page.evaluate(() => window.scrollY), "进入赛事详情应回到页面顶部").toBeLessThanOrEqual(1);
  await expect(page.getByRole("tab", { name: "赛事介绍" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileTabMetrics = await page.getByRole("tab").evaluateAll((tabs) => tabs.map((tab) => ({
    height: tab.getBoundingClientRect().height,
    whiteSpace: getComputedStyle(tab).whiteSpace,
  })));
  expect(mobileTabMetrics.every((tab) => tab.height <= 48 && tab.whiteSpace === "nowrap"), "手机端赛事栏目不应逐字换行").toBeTruthy();
  await page.getByRole("tab", { name: "规则说明" }).click();
  await expect(page.getByText(/文明发言/)).toBeVisible();
  await page.getByRole("tab", { name: "观战列表" }).click();
  await expect(page.getByText(/可观战的比赛|暂无可观战的公开比赛/).first()).toBeVisible();
  await expectNoHorizontalOverflow(page);
  expect(runtimeErrors).toEqual([]);
});

test("匿名观战保持只读、实时连接且舞台无横向溢出", async ({ page }, testInfo) => {
  const runtimeErrors = watchRuntimeErrors(page);
  const response = await page.request.get("api/live-rooms");
  expect(response.ok(), "公开房间 API 应可用").toBeTruthy();
  const payload = await response.json() as { items?: Array<{ code: string }> };
  const roomCode = payload.items?.[0]?.code;
  test.skip(!roomCode, "当前没有公开可观战房间");
  await page.goto(`rooms/${roomCode}/watch`, { waitUntil: "domcontentloaded" });

  await expect(page.getByText("观战模式", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("公开只读画面", { exact: true })).toBeVisible();
  await expect(page.getByText(new RegExp(`房间 ${roomCode}`))).toBeVisible();
  await expect(page.getByRole("status").first()).toContainText("实时连接", { timeout: 15_000 });
  await expect(page.locator(".site-footer")).toBeHidden();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, `watch-room-${roomCode}`);
  expect(runtimeErrors).toEqual([]);
});

test("匿名参赛先校验房间号，并可直接进入公开观战", async ({ page }) => {
  test.setTimeout(90_000);
  const runtimeErrors = watchRuntimeErrors(page);
  await page.goto("./", { waitUntil: "domcontentloaded" });
  const createCompetition = page.getByRole("button", { name: "创建 4v4 比赛" });
  await expect(createCompetition).toBeEnabled({ timeout: 20_000 });
  await createCompetition.click();
  const searchRoom = page.getByRole("button", { name: /搜索房间/ });
  await expect(searchRoom).toBeEnabled();
  await searchRoom.click();
  const roomCode = page.getByLabel("六位房间号");
  await expect(roomCode).toBeVisible();
  await roomCode.fill("000000");
  const enterRoom = page.getByRole("button", { name: "登录或注册后进入" });
  await expect(enterRoom).toBeEnabled();
  await enterRoom.click();
  await expect(page.locator(".participate-dialog .error-box[role='alert']")).toContainText(
    "房间不存在",
  );
  await expect(page).toHaveURL(/\/$/);
  // The public room preflight intentionally returns 404 for an unknown code.
  // Keep monitoring subsequent navigation without treating that expected response as a runtime fault.
  runtimeErrors.length = 0;

  const response = await page.request.get("api/live-rooms");
  expect(response.ok(), "公开房间 API 应可用").toBeTruthy();
  const payload = await response.json() as { items?: Array<{ code: string }> };
  const publicRoomCode = payload.items?.[0]?.code;
  if (publicRoomCode) {
    await roomCode.fill(publicRoomCode);
    await page.getByRole("button", { name: "登录或注册后进入" }).click();
    await expect(page).toHaveURL(new RegExp(`/rooms/${publicRoomCode}/watch$`));
    await expect(page.getByText("观战模式", { exact: true })).toBeVisible();
  }
  expect(runtimeErrors).toEqual([]);
});

test("退役的教师和课堂路由不再构成产品入口", async ({ page }) => {
  for (const path of ["teacher", "teacher/consents", "admin/classrooms"]) {
    const response = await page.goto(path, { waitUntil: "domcontentloaded" });
    expect(response?.status()).toBe(404);
    await expect(page.getByRole("heading", { name: "404" })).toBeVisible();
    await expect(page.getByText(/教师工作台|教学活动|学校与课堂/)).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  }
});

test("登录、注册和排行榜公开流程完整", async ({ page }, testInfo) => {
  const runtimeErrors = watchRuntimeErrors(page);
  await page.goto("login", { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "账号登录" })).toBeVisible();
  await expect(page.getByLabel("登录账号")).toBeVisible();
  await expect(page.getByLabel("密码")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page.getByRole("link", { name: "立即注册" }).click();
  await expect(page.getByRole("heading", { name: "注册参赛" })).toBeVisible();
  await expect(page.getByLabel("真实姓名")).toBeVisible();
  await expect(page.getByLabel("确认密码")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page.goto("rankings", { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "赛季排行榜" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "rankings");
  expect(runtimeErrors).toEqual([]);
});
