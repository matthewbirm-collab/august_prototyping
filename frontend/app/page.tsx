import { connection } from "next/server";
import { Suspense } from "react";

import { getHealth } from "@/lib/api";

async function BackendStatus() {
  await connection();
  const health = await getHealth();
  const up = health?.status === "ok";
  return (
    <p className="flex items-center gap-2" data-testid="backend-status">
      <span className={`h-2.5 w-2.5 rounded-full ${up ? "bg-emerald-500" : "bg-red-500"}`} />
      Backend {up ? "is up" : "is unreachable"}
    </p>
  );
}

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-4 px-4 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">propline-fantasy</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        Market-implied NFL fantasy projections from sportsbook player-prop lines.
      </p>
      <Suspense fallback={<p>Checking backend…</p>}>
        <BackendStatus />
      </Suspense>
    </main>
  );
}
