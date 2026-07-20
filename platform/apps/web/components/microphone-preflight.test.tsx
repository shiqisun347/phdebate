import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MicrophonePreflight } from "@/components/microphone-preflight";

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");

function restoreMediaDevices() {
  if (originalMediaDevices) Object.defineProperty(navigator, "mediaDevices", originalMediaDevices);
  else Reflect.deleteProperty(navigator, "mediaDevices");
}

function installWorkingMicrophone(sample: number) {
  const stop = vi.fn();
  const disconnect = vi.fn();
  const close = vi.fn().mockResolvedValue(undefined);
  const resume = vi.fn().mockResolvedValue(undefined);
  const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
  });
  class FakeContext {
    state: AudioContextState = "running";
    createMediaStreamSource() { return { connect: vi.fn(), disconnect }; }
    createAnalyser() {
      return {
        fftSize: 1024,
        getFloatTimeDomainData(values: Float32Array) { values.fill(sample); },
      };
    }
    resume = resume;
    close = close;
  }
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("MediaRecorder", class {});
  return { stop, disconnect, close, resume };
}

async function finishTest() {
  await act(async () => {
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(4_200);
  });
}

describe("MicrophonePreflight", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    restoreMediaDevices();
  });

  it("shows a low-frequency accessible level meter and releases resources after success", async () => {
    const resources = installWorkingMicrophone(0.1);
    render(<MicrophonePreflight />);
    fireEvent.click(screen.getByRole("button", { name: "测试默认麦克风" }));
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(320);
    });
    const meter = screen.getByRole("meter", { name: "麦克风输入音量" });
    expect(Number(meter.getAttribute("aria-valuenow"))).toBeGreaterThan(0);
    expect(meter).toHaveAttribute("aria-valuetext", "音量正常");

    await finishTest();
    expect(screen.getByRole("status")).toHaveTextContent("麦克风可用，已检测到声音");
    expect(resources.stop).toHaveBeenCalledOnce();
    expect(resources.disconnect).toHaveBeenCalledOnce();
    expect(resources.close).toHaveBeenCalledOnce();
  });

  it("explains a silent input and still releases every resource", async () => {
    const resources = installWorkingMicrophone(0);
    render(<MicrophonePreflight />);
    fireEvent.click(screen.getByRole("button", { name: "测试默认麦克风" }));
    await finishTest();

    expect(screen.getByRole("alert")).toHaveTextContent("未检测到清晰声音");
    expect(screen.getByRole("alert")).toHaveTextContent("确认设备未静音");
    expect(resources.stop).toHaveBeenCalledOnce();
    expect(resources.close).toHaveBeenCalledOnce();
  });

  it.each([
    ["NotAllowedError", "麦克风权限被拒绝"],
    ["NotFoundError", "未找到可用麦克风"],
    ["NotReadableError", "正被其他应用占用"],
  ])("shows actionable Chinese guidance for %s", async (name, expected) => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new DOMException("failed", name)) },
    });
    vi.stubGlobal("AudioContext", class {});
    vi.stubGlobal("MediaRecorder", class {});
    render(<MicrophonePreflight />);
    fireEvent.click(screen.getByRole("button", { name: "测试默认麦克风" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent(expected);
    expect(screen.getByRole("button", { name: "测试默认麦克风" })).toBeEnabled();
  });

  it("reports unsupported browsers without requesting a device", async () => {
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    vi.stubGlobal("MediaRecorder", undefined);
    vi.stubGlobal("AudioContext", undefined);
    render(<MicrophonePreflight />);
    fireEvent.click(screen.getByRole("button", { name: "测试默认麦克风" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent("当前浏览器不支持比赛所需的麦克风录音");
  });

  it("releases an opened track and AudioContext when analysis setup fails", async () => {
    const stop = vi.fn();
    const close = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] }) },
    });
    class FailingContext {
      state: AudioContextState = "running";
      resume = vi.fn().mockResolvedValue(undefined);
      close = close;
      createMediaStreamSource() { throw new Error("analysis unavailable"); }
    }
    vi.stubGlobal("AudioContext", FailingContext);
    vi.stubGlobal("MediaRecorder", class {});
    render(<MicrophonePreflight />);
    fireEvent.click(screen.getByRole("button", { name: "测试默认麦克风" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent("麦克风测试失败");
    expect(stop).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });
});
