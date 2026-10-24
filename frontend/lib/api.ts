// Base URL of the FastAPI backend. Server-side only; set API_URL in .env.
export const API_URL = process.env.API_URL ?? "http://localhost:8000";

export type Health = { status: "ok" };

export async function getHealth(): Promise<Health | null> {
  try {
    const response = await fetch(`${API_URL}/health`, { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as Health;
  } catch {
    return null;
  }
}
