export function Footer() {
  return (
    <footer className="border-t border-zinc-200 px-4 py-6 text-sm text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
      <p>Projections only — not betting advice.</p>
      <p className="mt-1">
        NFL data from{" "}
        <a className="underline" href="https://github.com/nflverse">
          nflverse
        </a>{" "}
        (CC-BY 4.0; FTN charting data CC-BY-SA 4.0). Odds data from The Odds API.
      </p>
    </footer>
  );
}
