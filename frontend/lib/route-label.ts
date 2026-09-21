import type { TurnRoute } from "./types.ts"

export function routeLabel(route: TurnRoute | undefined | null): string | null {
  if (!route) return null
  if (route.workflow === "converse") return null
  if (route.workflow === "inspect" || route.workflow === "direct") return "Answered directly"
  if (route.workflow === "agentic" && route.plan === "none") return "Investigated"
  if (route.workflow === "agentic" && (route.plan === "self" || route.plan === "gated")) return "Planned, then investigated"
  if (route.workflow === "plan_only") return "Plan only"
  if (route.workflow === "execute_plan") return "Ran the approved plan"
  return null
}
