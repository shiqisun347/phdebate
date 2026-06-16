import { KeyRound } from "lucide-react";
import type { FormEvent } from "react";
import { useState } from "react";
import { type AuthRole, saveAuthToken } from "../api/client";

interface AuthPromptProps {
  role: AuthRole;
  speakerId?: string;
  message?: string | null;
}

const roleLabel: Record<AuthRole, string> = {
  admin: "管理/主持人口令",
  host: "主持导播口令",
  screen: "大屏只读 token",
  speaker: "辩手临时 token"
};

export function AuthPrompt({ role, speakerId, message }: AuthPromptProps) {
  const [token, setToken] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!token.trim()) return;
    saveAuthToken(role, token, speakerId);
    window.location.reload();
  }

  return (
    <main className="auth-shell">
      <form className="auth-card" onSubmit={submit}>
        <KeyRound size={28} />
        <h1>{roleLabel[role]}</h1>
        {message && <p>{message}</p>}
        <input
          autoFocus
          type="password"
          value={token}
          placeholder="输入现场发放的访问口令"
          onChange={(event) => setToken(event.target.value)}
        />
        <button type="submit">进入</button>
      </form>
    </main>
  );
}
