# KEP-968: Auto Suspend/Resume for Sandboxes

<!--
TOC is auto-generated via `make toc-update`.
-->

<!-- toc -->
- [Summary](#summary)
- [Motivation](#motivation)
  - [Goals](#goals)
  - [Non-Goals](#non-goals)
- [Proposal](#proposal)
  - [User Stories](#user-stories)
  - [High-Level Design](#high-level-design)
    - [API Changes](#api-changes)
    - [Implementation Guidance](#implementation-guidance)
- [Scalability](#scalability)
- [Implementation Plan](#implementation-plan)
- [Migration Plan](#migration-plan)
- [Alternatives](#alternatives)
<!-- /toc -->

## Summary

This KEP adds automatic, idle-based suspension of Sandboxes and automatic resume on
incoming traffic. A new `spec.lifecycle.idlePolicy` field declares how long a
sandbox may be inactive before the controller suspends it (via the existing
`spec.operatingMode: Suspended` from KEP-694) or expires it. Activity is reported
through a per-sandbox `coordination.k8s.io/v1` Lease, renewable by the
sandbox-router (data-path traffic), by SDKs (API invocations), and optionally by
the workload itself (long-running background work). The sandbox-router gains an
opt-in resume path: a request targeting a suspended sandbox patches
`operatingMode: Running`, holds the request until the backing Pod is ready, and
then forwards it. This implements the roadmap items "Auto Suspend/Resume" and
"Scale to Zero".

## Motivation

Idle sandboxes hold their full resource footprint. For AI-agent platforms,
sessions are bursty: a sandbox is used intensively for minutes and then sits idle
for hours, but must come back with its filesystem intact. Manual suspend/resume
(KEP-694) exists but requires an external orchestrator to watch activity and flip
`operatingMode` — every adopter rebuilds the same idle-detection loop, and none
can do wake-on-traffic without proxy cooperation.

### Goals

- Declarative per-sandbox idle policy: suspend or expire after N seconds of
  inactivity.
- An activity channel covering all three sources identified in #968: inbound
  data-path HTTP (router), SDK/API invocations, and workload-asserted busy
  (background work with no inbound traffic).
- Automatic resume triggered from the data path, holding the triggering request.
- No measurable etcd/API-server load increase per request (activity reporting is
  debounced; Lease renewals do not trigger reconciles).
- Converge with the idle lifecycle policy discussion (#849) into a single config
  surface.

### Non-Goals

- Memory-state preservation across suspend (PVC-only semantics per KEP-694;
  snapshot integration remains provider-specific).
- Warm-pool scale-to-zero via KEDA (#677) — pool-level, already possible via
  `/scale`.
- A generic activity/metrics pipeline; the Lease is a control signal, not
  telemetry.

## Proposal

### User Stories

1. **Multi-tenant coding agents:** each tenant has a singleton PVC-backed
   sandbox. The operator sets `idlePolicy: {idleTimeoutSeconds: 900}` in the
   SandboxTemplate; idle tenant sandboxes suspend, and the tenant's next HTTP
   request through the router transparently resumes them.
2. **RL training (bursty fleets):** short-lived sandboxes get
   `idlePolicy: {idleTimeoutSeconds: 300, action: Delete}` — expired at the idle
   deadline with no intervening suspend; inactivity-based cleanup without a
   `shutdownTime` guess.
3. **Agent running a long build:** no inbound traffic for 30 minutes, but the
   workload renews the activity Lease; the sandbox is not suspended mid-build.
4. **Health checks:** a probe sends `X-Sandbox-No-Resume: 1` and gets a 503 for a
   suspended sandbox instead of waking it.

### High-Level Design

Three cooperating parts — sensor, policy, actuator:

```
  router / SDKs / workload ── renew (debounced) ──► Lease sandbox-activity-<hash>
                                                        │ renewTime read on reconcile
                                                        ▼
  sandbox controller: idleDeadline = max(renewTime, createdAt, lastResumeTime)
                                     + idleTimeout;  RequeueAfter(deadline)
        ▼ deadline passed
  action: Suspend → patch operatingMode: Suspended (KEP-694 machinery:
                    pod deleted, PVC + Service kept); optionally reclaim
                    after suspendedTTLSeconds without a resume
  action: Delete  → expire immediately under the owner's shutdownPolicy,
                    no Suspended transition

  ── resume path (router, opt-in) ──
  request → Pod-IP cache miss → Sandbox suspended → singleflight PATCH
  operatingMode: Running → hold request until Pod IP appears → forward
  suspend in flight (PodTerminating) → 503 + Retry-After (never reverse it)
```

#### API Changes

Extend the existing `Lifecycle` struct (`api/v1beta1/sandbox_types.go`), keeping
`shutdownTime`/`shutdownPolicy` untouched:

```go
type Lifecycle struct {
    ShutdownTime   *metav1.Time    `json:"shutdownTime,omitempty"`   // existing
    ShutdownPolicy *ShutdownPolicy `json:"shutdownPolicy,omitempty"` // existing

    // idlePolicy configures automatic suspension or expiry of inactive sandboxes.
    // +optional
    IdlePolicy *IdlePolicy `json:"idlePolicy,omitempty"`
}

type IdlePolicy struct {
    // Suspend or expire after this many seconds without observed activity.
    // +required
    IdleTimeoutSeconds int32 `json:"idleTimeoutSeconds"`

    // What to do at the idle deadline. Suspend (default) sets
    // operatingMode: Suspended. Delete expires the sandbox immediately with no
    // intervening suspend — "expire now", NOT "force object deletion": the
    // owner's shutdownPolicy decides whether the API object is deleted or
    // retained as terminal.
    // +kubebuilder:validation:Enum=Suspend;Delete
    // +kubebuilder:default=Suspend
    // +optional
    Action IdleAction `json:"action,omitempty"`

    // How long a sandbox suspended by this policy may stay suspended before it
    // is expired (two-stage lifecycle per #849:
    // active → Suspended → reclaimed). Resume resets both stages.
    // Only valid with action: Suspend. Nil = retained indefinitely.
    // +optional
    SuspendedTTLSeconds *int32 `json:"suspendedTTLSeconds,omitempty"`

    // Automatic resume triggers: Traffic (router resumes on inbound request,
    // default) or None (only explicit operatingMode patches resume).
    // +kubebuilder:default={"Traffic"}
    // +optional
    ResumeOn []ResumeTrigger `json:"resumeOn,omitempty"`

    // Enabled lets the workload assert "busy" by renewing the activity Lease;
    // the controller then creates a per-sandbox Role/RoleBinding scoped to that
    // one Lease. Default Disabled.
    // +kubebuilder:validation:Enum=Enabled;Disabled
    // +kubebuilder:default=Disabled
    // +optional
    WorkloadActivity WorkloadActivityPolicy `json:"workloadActivity,omitempty"`
}
```

**Template/claim propagation.** `SandboxBlueprint` deliberately excludes
`lifecycle`, so a field only on `Sandbox.spec.lifecycle` would be unreachable from
templates and claims. Therefore `SandboxTemplateSpec` gains `idlePolicy` (outside
the embedded blueprint, beside `networkPolicy`, so it never affects warm-pool
staleness hashing) plus an `idlePolicyOverridePolicy: Allowed|Disallowed` knob
(mirroring `envVarsInjectionPolicy`), and `SandboxClaimSpec.Lifecycle` gains an
overriding `idlePolicy`. The claim controller writes the winning value onto the
Sandbox at claim time. **Unclaimed warm-pool members never receive an idle
policy** — pre-warmed standby capacity must not self-suspend.

**Status additions** (written on transitions only, not per-renewal):
`lastActivityTime` (copied from the Lease at suspend time), `lastResumeTime`
(stamped on the condition-keyed resume transition; the idle baseline), and
`idleReclaimAfter` (the pending reclaim deadline; on claim-owned sandboxes it is
also the handoff signal to the claim controller — see below). The `Suspended`
condition keeps the KEP-119 physical-state reasons unchanged; the idle *cause* is
conveyed by events, `lastActivityTime`, and — durably, since Events expire — an
idle-specific *message* on the existing generic terminal condition reason when a
Retain expiry was idle-caused.

**Activity Lease.** `sandbox-activity-<nameHash>`, created by the controller with
an ownerReference, GC'd with the sandbox. `spec.renewTime` is the activity
signal. Writers: router (debounced, default ≥15s per sandbox), SDKs, and — with
`workloadActivity: Enabled` — the workload, via a controller-created per-sandbox
Role/RoleBinding restricted by `resourceNames` to that sandbox's own Lease (a
compromised workload can only keep *itself* alive, bounded by `shutdownTime`).
This is the Lease-based channel suggested in #849.

**v1alpha1 conversion.** New spec fields round-trip via dedicated annotations
using the existing `v1beta1-volume-claim-templates-policy` down-convert-stash /
up-convert-restore pattern, on all three surfaces (Sandbox, SandboxTemplate,
SandboxClaim). New status fields are not stashed (controller-owned, recomputable
for live objects).

#### Implementation Guidance

**Sandbox controller.** `checkSandboxIdle` folds into the existing
deadline→RequeueAfter pattern beside `checkSandboxExpiry`. Lease renewals are
predicate-filtered out of reconcile triggers; timing comes from RequeueAfter.
Key behaviors:

- *Auto-suspend* patches `operatingMode: Suspended` with a distinct field manager
  (`sandbox-idle-controller`) so GitOps can detect automation (document Argo/Flux
  `ignoreDifferences`).
- *Precedence:* a deletion timestamp wins first; static expiry (`shutdownTime`)
  beats idle policy; a user patch to `Running` after idle-suspend is a resume and
  resets the baseline; a user `operatingMode` patch does not cancel an
  already-due `action: Delete` expiry — only new activity does.
- *Bootstrap warmup:* for a grace period after controller start, the idle
  baseline is at least the controller start time — otherwise a controller
  restart mass-suspends every quiet-but-alive sandbox.
- *Resume* stamps `lastResumeTime`, clears `idleReclaimAfter`, and renews the
  Lease so the sweeper doesn't immediately re-suspend.
- *Source-aware expiry:* expiry evaluation returns which deadline won
  (shutdownTime vs idle) so idle reclaim and ordinary expiry emit distinct
  events (`SandboxAutoSuspended`, `SandboxAutoReclaimed` — named constants,
  emitted before deletion, idempotent on repeated reconciles) and distinct
  terminal condition messages, without new condition reasons.

**SandboxClaim controller (claim-owned expiry).** Deleting a claim-owned Sandbox
directly would fight the claim controller, which recreates it. Instead the
sandbox controller writes `status.idleReclaimAfter` as a handoff, and the claim
controller folds it into its existing expiration computation as one more
candidate deadline (earliest wins) — expiring the claim through the existing,
tested claim-expiry flow under the claim's `shutdownPolicy`. The pre-expiry
Sandbox lookup is read-only and trusts the marker only after
`metav1.IsControlledBy(sandbox, claim)`.

**Warm-pool fix (required, same series).** The pool controller GC-deletes any
unclaimed sandbox that is not Ready past `warmPoolReadinessGracePeriod`; a
suspended sandbox is `Ready=False/SandboxSuspended` and would be deleted. Exempt
`Suspended=True` from the stuck-check (claim-time policy application already
prevents auto-suspension of unclaimed members; this covers manual suspends).

**Router (all opt-in via `--enable-auto-resume`, requires `--cache-enabled`).**
Renews the Lease per proxied request (debounced). On a Pod-IP cache miss for a
suspended sandbox with `resumeOn: Traffic`: singleflight-patch
`operatingMode: Running`, hold the request until a live Pod-IP cache entry
appears, then forward; `--resume-timeout` (default 60s — resume is a cold pod
start) returns 504. **A request arriving while suspension is in flight
(`Suspended` reason `PodTerminating`) gets 503 + Retry-After and never patches** —
otherwise traffic mid-suspend reverses it and the sandbox thrashes. This is why
KEP-119's persistent Suspended condition (PR #1150) is a hard prerequisite.
`X-Sandbox-No-Resume: 1` bypasses waking. New opt-in RBAC (sandboxes
get/list/watch/patch + leases); recommend requiring `--authz-mode=tokenreview`
when enabled. New metrics: resume attempts/latency, activity renewals; plus a
resume-latency histogram controller-side and a `suspended` label on the
`agent_sandboxes` gauge.

**SDKs (phase 2).** Go/Python clients resume-if-suspended before connecting; both
already re-resolve pod IPs across suspend/resume (#957).

## Scalability

- **API-server writes:** debounced Lease renewals cap at 1 write/sandbox/interval
  regardless of request rate (10,000 *actively trafficked* sandboxes at 15s ≈
  667 writes/s worst case; typical fleets are mostly idle). Renewals are filtered
  from reconcile triggers, so controller QPS is unaffected.
- **Reconcile load:** one extra RequeueAfter per sandbox per idle period; no
  per-request controller work.
- **Router hot path:** one debounce-map lookup per request; the resume path
  activates only on cache misses, and singleflight bounds a thundering herd to a
  single PATCH.
- **Validation:** extend `test/stress/` to confirm Lease write volume and absence
  of reconcile storms at 1000+ sandboxes.

## Implementation Plan

Staged, independently mergeable PRs:

1. **Prerequisite:** KEP-119 persistent Suspended conditions land first
   (PR #1150, in flight — coordinate, don't duplicate). The router's anti-thrash
   decision depends on the `PodTerminating`/`PodTerminated` reasons.
2. **API + sandbox controller:** `IdlePolicy` types, CRD regeneration, activity
   Lease lifecycle, `checkSandboxIdle` + requeue, auto-suspend patch, status
   fields, events. Includes the warm-pool GC exemption for `Suspended=True`.
3. **Template/claim propagation:** extension API fields, claim-time write-through,
   claim-controller expiry integration (`idleReclaimAfter` handoff), webhook
   validation.
4. **Router auto-resume:** flag-gated Lease renewal + singleflight resume path,
   new opt-in RBAC manifest, metrics. Off by default; behavior with the flag off
   is byte-identical to today.
5. **SDKs + docs:** resume-if-suspended in Go/Python clients, site lifecycle docs.

Testing per stage: envtest for controller transitions (idle→suspend, renewal
defers, resume resets, controller-restart warmup), router unit tests (singleflight,
timeout, flag-off golden path), kind e2e (suspend on idle → wake via router on one
held request), and a `test/stress/` run validating Lease write volume and absence
of reconcile storms at 1000+ sandboxes.

## Migration Plan

- All new fields are optional with defaults; existing Sandboxes/Templates/Claims
  without `idlePolicy` behave exactly as today. No storage migration.
- v1alpha1 clients: new spec fields round-trip via conversion annotations (see API
  Changes); without the annotation the fields simply don't exist in v1alpha1 —
  no behavior change for v1alpha1-only workflows.
- Router upgrades are independent: an old router with a new controller means no
  wake-on-traffic (suspended sandboxes return 502 as today) until the router is
  upgraded and the flag enabled; a new router with the flag off is unchanged.
- Rollback: disabling the router flag and removing `idlePolicy` from specs fully
  restores current behavior; suspended sandboxes remain resumable via the existing
  manual `operatingMode` patch.

## Alternatives

| Alternative | Verdict | Why |
|---|---|---|
| Activity via annotation/status writes on the Sandbox per request | Rejected | Full object writes → etcd churn + spurious reconciles; Leases exist for heartbeats. |
| Router-side idle detection (router suspends) | Rejected | Router only sees inbound traffic — misses workload-asserted busy (#968); policy belongs to the owning controller. Router only ever resumes. |
| KEDA/HPA on a per-Sandbox `/scale` | Rejected | v1beta1 deliberately removed `spec.replicas` (KEP-694); KEDA remains the answer for warm pools (#677). |

Also considered and set aside: a Knative-style activator (deferred — cleanest at
very large scale but adds a new deployable), node-level eBPF idle detection
(runtime-specific), and a separate `SandboxIdlePolicy` CRD (lifecycle config
belongs on `lifecycle`).

Open API-review questions: `resumeOn` list vs single enum; `IdleAction: Delete`
naming (`Expire`/`Reclaim` may be more precise — kept as `Delete` to match
#968/roadmap language); default `--resume-timeout`; whether Scale to Zero should
be tracked as a separate KEP.
