# Wizard — GitHub Org Migration (for Claude Code to execute)

**Precondition:** the GitHub organization must already exist (see the manual steps given separately in chat — this cannot be done via CLI). Once it exists, everything below can be run from the terminal.

Run this **phase by phase, not all at once** — confirm each phase's output before moving to the next. Phase 2 (repo transfer) is not easily reversible; don't proceed past it without the person's explicit go-ahead.

Fill in these three variables once, at the top of your session, before running anything:

```bash
ORG="Wizard-AIA"              # the org name, exactly as created — replace this
REPO_NAME="Wizard-w1"        # current repo name
OLD_OWNER="Aniket-a14"       # current personal account
```

---

## Phase 0 — Preconditions check

```bash
gh auth status                         # confirms gh is authenticated
gh api "orgs/$ORG" --silent && echo "Org exists and is reachable" || echo "STOP: org not found — it must be created manually first"
```

If the second command fails, stop here and tell the person the org isn't ready yet.

---

## Phase 1 — Transfer the repository into the org

```bash
gh api "repos/$OLD_OWNER/$REPO_NAME/transfer" -f new_owner="$ORG"
```

This preserves stars, issues, PRs, and watchers, and GitHub auto-redirects the old URL. If this call fails with a permissions error, the person's account may need to confirm the transfer manually via a GitHub email/notification — flag that back to them rather than retrying blindly.

---

## Phase 2 — Point the local clone at the new location

```bash
git remote set-url origin "https://github.com/$ORG/$REPO_NAME.git"
git remote -v   # verify it now points at the org
```

---

## Phase 3 — Update repo metadata

```bash
gh repo edit "$ORG/$REPO_NAME" \
  --description "Local-first autonomous data analysis agent" \
  --add-topic agent \
  --add-topic agentic-ai \
  --add-topic data-analysis \
  --add-topic data-science \
  --enable-discussions \
  --enable-issues
```

---

## Phase 4 — Create the organization profile repo

This special repo (named exactly like the org) renders as the org's front page on GitHub.

```bash
gh repo create "$ORG/$ORG" --public --description "Wizard — organization profile"
git clone "https://github.com/$ORG/$ORG.git" /tmp/wizard-org-profile
cd /tmp/wizard-org-profile
```

Create `README.md` with content like:

```markdown
# Wizard

A local-first autonomous data analysis agent. Ask a real question about your data;
it investigates — looking, computing, revising its approach when the data disagrees
with it — then verifies the result and explains it.

- [Wizard](https://github.com/ORG_PLACEHOLDER/Wizard-w1) — the core engine
- [Docs](https://github.com/ORG_PLACEHOLDER/docs) — documentation site source
- [Skills](https://github.com/ORG_PLACEHOLDER/skills) — the community skill registry

MIT/BSD-licensed. See CONTRIBUTING.md in the core repo to get involved.
```

(Replace `ORG_PLACEHOLDER` with the real org name.)

```bash
git add README.md
git commit -m "Add organization profile README"
git push
cd -
```

---

## Phase 5 — Create satellite repos

```bash
gh repo create "$ORG/docs" --public --description "Wizard documentation site"
gh repo create "$ORG/skills" --public --description "Community skill registry for Wizard"
```

---

## Phase 6 — Governance files in the core repo

Create these files if they don't already exist (`CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `LICENSE` already exist per the current repo — just update any hardcoded old-owner URLs in them to the new org).

**`MAINTAINERS.md`**
```markdown
# Maintainers

| Name | Role | GitHub |
|------|------|--------|
| (your name) | Lead maintainer | @OLD_OWNER_PLACEHOLDER |

New maintainers are added by consensus of existing maintainers. See CONTRIBUTING.md
for how to get involved.
```

**`.github/ISSUE_TEMPLATE/bug_report.md`**
```markdown
---
name: Bug report
about: Report something that isn't working
labels: bug
---

**Describe the bug**
A clear description of what's wrong.

**To reproduce**
Steps to reproduce the behavior.

**Expected behavior**
What you expected to happen instead.

**Environment**
- OS:
- Execution backend (docker/local/inprocess):
- Model provider(s):
```

**`.github/ISSUE_TEMPLATE/feature_request.md`**
```markdown
---
name: Feature request
about: Suggest an idea for Wizard
labels: enhancement
---

**Problem**
What are you trying to do that Wizard doesn't support today?

**Proposed solution**
What you'd like to see.

**Alternatives considered**
Any workarounds you've tried.
```

**`.github/PULL_REQUEST_TEMPLATE.md`**
```markdown
## Problem

## Solution

## Testing
- [ ] `pytest` passes locally
- [ ] `ruff check . --fix && ruff format .` clean
- [ ] Frontend: `pnpm lint && npx tsc --noEmit && pnpm build` clean (if frontend touched)

## Related issue
Closes #
```

Commit and push these:

```bash
git add MAINTAINERS.md .github/ISSUE_TEMPLATE/ .github/PULL_REQUEST_TEMPLATE.md
git commit -m "Add governance files: maintainers, issue templates, PR template"
git push
```

---

## Phase 7 — Branch protection

```bash
gh api "repos/$ORG/$REPO_NAME/branches/master/protection" \
  --method PUT \
  -f required_status_checks.strict=true \
  -F "required_status_checks.contexts[]=ci" \
  -F "required_status_checks.contexts[]=codeql" \
  -F required_pull_request_reviews.required_approving_review_count=1 \
  -F enforce_admins=false
```

Adjust the status-check context names (`ci`, `codeql`) to match your actual workflow job names — check with:

```bash
gh api "repos/$ORG/$REPO_NAME/commits/master/check-runs" --jq '.check_runs[].name'
```

---

## Phase 8 — First release under the new org

```bash
gh release create "v2.0.0-w2-planning" \
  --repo "$ORG/$REPO_NAME" \
  --title "Wizard w2 — evolution roadmap kickoff" \
  --notes "First release under the $ORG organization. See docs/wizard-evolution-spec.md for the w2 milestone roadmap."
```

---

## What this script does NOT cover

Repo secrets (Phase 9, not included above) need real credential values that shouldn't be typed into an AI session. Run this yourself, interactively, when needed:

```bash
gh secret set SECRET_NAME --repo "$ORG/$REPO_NAME"
# prompts for the value on stdin, doesn't echo it, doesn't touch chat history
```
