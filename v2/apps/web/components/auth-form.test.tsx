import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthForm } from "@/components/auth-form";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }));
const searchState = vi.hoisted(() => ({ value: "" }));
const sessionState = vi.hoisted(() => ({ user: null as { id: string } | null, loading: false }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
  useSearchParams: () => new URLSearchParams(searchState.value),
}));

vi.mock("@/lib/use-session", () => ({
  notifySessionChanged: vi.fn(),
  useSession: () => sessionState,
}));

function setVisibleValueWithoutReactChange(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
}

describe("AuthForm", () => {
  afterEach(() => {
    navigation.push.mockReset();
    navigation.replace.mockReset();
    navigation.refresh.mockReset();
    searchState.value = "";
    sessionState.user = null;
    sessionState.loading = false;
    vi.unstubAllGlobals();
  });

  it("redirects an already authenticated user instead of allowing a second account to be registered", async () => {
    sessionState.user = { id: "existing-user" };
    render(<AuthForm mode="register" />);

    expect(screen.queryByLabelText("登录账号")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("你已登录");
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/me"));
  });

  it("submits visible Safari autofill values even when React change events did not fire", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ user: { id: "user" } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthForm mode="register" />);

    setVisibleValueWithoutReactChange(screen.getByLabelText("登录账号"), "safari_user");
    setVisibleValueWithoutReactChange(screen.getByLabelText("真实姓名"), "Safari 测试用户");
    setVisibleValueWithoutReactChange(screen.getByLabelText("密码"), "Password-1234");
    setVisibleValueWithoutReactChange(screen.getByLabelText("确认密码"), "Password-1234");
    fireEvent.submit(screen.getByRole("button", { name: "注册并进入赛场" }).closest("form")!);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      account: "safari_user",
      real_name: "Safari 测试用户",
      password: "Password-1234",
      confirm_password: "Password-1234",
    });
    expect(navigation.push).toHaveBeenCalledWith("/");
  });

  it("renders FastAPI validation errors as readable text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [{ type: "missing", loc: ["body", "password"], msg: "Field required" }],
    }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    })));
    render(<AuthForm mode="login" />);
    fireEvent.change(screen.getByLabelText("登录账号"), { target: { value: "missing_user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "Password-1234" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("密码为必填项。");
    expect(screen.getByRole("alert")).not.toHaveTextContent("[object Object]");
  });

  it("preserves the safe return path when switching between login and registration", () => {
    searchState.value = "next=%2Frooms%2F381526%2Flobby";
    const { rerender } = render(<AuthForm mode="login" />);
    expect(screen.getByRole("link", { name: "立即注册" })).toHaveAttribute(
      "href",
      "/register?next=%2Frooms%2F381526%2Flobby",
    );
    rerender(<AuthForm mode="register" />);
    expect(screen.getByRole("link", { name: "返回登录" })).toHaveAttribute(
      "href",
      "/login?next=%2Frooms%2F381526%2Flobby",
    );
  });
});
