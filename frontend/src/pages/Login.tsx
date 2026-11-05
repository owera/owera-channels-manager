export default function Login() {
  return (
    <div className="min-h-dvh flex items-center justify-center p-6 bg-ink-900">
      <div className="panel max-w-md w-full p-8 text-center">
        <div className="flex items-center justify-center gap-2 mb-2">
          <span className="w-2.5 h-2.5 bg-signal rounded-sm shadow-glow" />
          <span className="font-display font-extrabold text-fog-50 text-lg tracking-tight">
            Owera <span className="text-signal">Channels Manager</span>
          </span>
        </div>
        <p className="text-sm text-fog-400 mb-8">Sign in with your Owera Google account to continue.</p>
        <a
          href="/api/auth/google/start"
          className="btn inline-flex items-center justify-center gap-2 w-full"
        >
          Sign in with Google
        </a>
      </div>
    </div>
  );
}
