/**
 * Settings → Models. Spec §7.7.6 details overrides; Phase 1 scaffolds a
 * placeholder so the left-nav item resolves.
 */
export default function ModelsSettingsPage() {
  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="font-serif text-2xl font-semibold tracking-tight text-foreground">
          Models
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-feature model selection and cost-tier defaults. Ships with the
          LLM plumbing in a later phase.
        </p>
      </header>
      <div className="border border-border bg-subtle px-6 py-8 text-sm text-muted-foreground">
        Not yet available. The defaults from spec §7.7.6 are baked in and will
        become user-overridable once real LLM calls are wired.
      </div>
    </div>
  );
}
