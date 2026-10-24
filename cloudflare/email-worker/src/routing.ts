// Pure helpers (no Workers APIs) so they can run under `node --test`.

const INSTANCE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export type Route = { instance: string; url: string } | { error: string };

/**
 * `plans@d` → default instance, `plans+laptop@d` → instance "laptop". The instance is substituted
 * into a template pinned to our tailnet's domain, so a recipient can never steer the Worker to an
 * arbitrary host.
 */
export function route(recipient: string, template: string, defaultInstance: string): Route {
  const local = recipient.trim().toLowerCase().split("@")[0] ?? "";
  const plus = local.indexOf("+");
  const instance = plus === -1 ? defaultInstance : local.slice(plus + 1);
  if (!INSTANCE.test(instance)) return { error: `"${instance}" is not a valid instance name` };
  return { instance, url: template.replace("{instance}", instance) };
}
