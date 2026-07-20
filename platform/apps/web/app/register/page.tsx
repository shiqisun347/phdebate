import { Suspense } from "react";
import { AuthForm } from "@/components/auth-form";

export default function RegisterPage() { return <div className="auth-shell"><div className="auth-copy"><span className="eyebrow">Create Identity</span><h1>用真实姓名<br /><span className="gradient-text">留下思辨轨迹</span></h1><p>无需邮箱或手机号。账号用于登录，真实姓名用于比赛、排行榜和历史记录。</p></div><div className="panel auth-card"><h2>注册参赛</h2><p>创建你的稷下辩论身份</p><Suspense><AuthForm mode="register" /></Suspense></div></div>; }

