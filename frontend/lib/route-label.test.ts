import test from "node:test"
import assert from "node:assert/strict"
import { routeLabel } from "./route-label.ts"
import type { TurnRoute } from "./types.ts"

test("routeLabel returns null for undefined or null", () => {
  assert.equal(routeLabel(undefined), null)
  assert.equal(routeLabel(null), null)
})

test("routeLabel returns null for converse", () => {
  const route = { workflow: "converse" } as TurnRoute
  assert.equal(routeLabel(route), null)
})

test("routeLabel returns Answered directly for inspect and direct", () => {
  const route1 = { workflow: "inspect" } as TurnRoute
  const route2 = { workflow: "direct" } as TurnRoute
  assert.equal(routeLabel(route1), "Answered directly")
  assert.equal(routeLabel(route2), "Answered directly")
})

test("routeLabel returns Investigated for agentic with plan none", () => {
  const route = { workflow: "agentic", plan: "none" } as TurnRoute
  assert.equal(routeLabel(route), "Investigated")
})

test("routeLabel returns Planned, then investigated for agentic with plan self or gated", () => {
  const route1 = { workflow: "agentic", plan: "self" } as TurnRoute
  const route2 = { workflow: "agentic", plan: "gated" } as TurnRoute
  assert.equal(routeLabel(route1), "Planned, then investigated")
  assert.equal(routeLabel(route2), "Planned, then investigated")
})

test("routeLabel returns Plan only for plan_only", () => {
  const route = { workflow: "plan_only" } as TurnRoute
  assert.equal(routeLabel(route), "Plan only")
})

test("routeLabel returns Ran the approved plan for execute_plan", () => {
  const route = { workflow: "execute_plan" } as TurnRoute
  assert.equal(routeLabel(route), "Ran the approved plan")
})
