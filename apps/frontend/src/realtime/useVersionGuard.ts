import { useEffect } from "react";

// 读取本页面实际加载的入口 bundle 文件名（含内容 hash），用于与服务器最新部署比对。
export function loadedBundle(): string {
  const scripts = Array.from(document.querySelectorAll<HTMLScriptElement>("script[src]"));
  for (const s of scripts) {
    const m = s.src.match(/(index-[A-Za-z0-9_]+\.js)/);
    if (m) return m[1];
  }
  return "";
}

/**
 * 版本守卫：定期向 /api/version 询问当前部署的入口 bundle；若与本页加载的不一致，说明这是
 * 一块在新版部署前就打开、之后没刷新的旧标签页——自动整页刷新加载新版。彻底杜绝"旧缓存
 * bundle 仍在跑"导致的播放异常（如音频取不到、连环跳）。
 *
 * 安全性：只在「成功拿到服务器版本 且 与本页不同」时刷新一次（随即停止轮询），绝不会因为
 * 网络抖动或版本一致而反复刷新；开发环境（无 hash bundle）自动不启用。
 */
export function useVersionGuard(intervalMs = 60000): void {
  useEffect(() => {
    const mine = loadedBundle();
    if (!mine) return; // dev / 无 hash bundle：不启用
    let stopped = false;
    const check = async () => {
      if (stopped) return;
      try {
        const res = await fetch("/api/version", { cache: "no-store" });
        if (!res.ok) return;
        const json = (await res.json()) as { data?: { bundle?: string } };
        const server = json?.data?.bundle ?? "";
        if (server && server !== mine) {
          stopped = true;
          window.location.reload();
        }
      } catch {
        /* 网络抖动：忽略，下个周期再查 */
      }
    };
    const initial = window.setTimeout(check, 8000);
    const timer = window.setInterval(check, intervalMs);
    return () => {
      stopped = true;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [intervalMs]);
}
