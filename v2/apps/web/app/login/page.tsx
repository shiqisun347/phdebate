import { Suspense } from "react";
import { AuthForm } from "@/components/auth-form";

export default function LoginPage() { return <div className="auth-shell"><div className="auth-copy"><span className="eyebrow">Welcome Back</span><h1>回到你的<br /><span className="gradient-text">辩论赛场</span></h1><p>登录后可继续中断的比赛、查看完整辩论历史，并与真人和 AI 再次交锋。</p></div><div className="panel auth-card"><h2>账号登录</h2><p>使用登录账号和密码进入平台</p><Suspense><AuthForm mode="login" /></Suspense></div></div>; }

