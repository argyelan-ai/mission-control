# Privacy sweep — what is still in here, and why it is written down

Mission Control grew out of one person's machine before it became a public
repository, and the traces of that machine are still in the tree: agent names
from the author's own fleet, mostly in comments, examples, migration data and
docs that were written when there was only ever one installation.

None of it is dangerous on its own. Together it is somebody's private setup,
published — and five commits had already shipped the whole fleet compose file
before anyone noticed. So the point of this page is not to feel bad about the
backlog. It is to make sure the backlog can only ever get **shorter**.

**252 tracked files** still carry at least one of the four names
(`sparky`, `shakespeare`, `freecode`, `davinci`):

| where | files |
|---|---|
| Tests and fixtures | 113 |
| Code and scripts | 83 |
| ADRs (dated records of real decisions) | 21 |
| Migrations (they carry the data as it was) | 18 |
| Frontend | 9 |
| Documentation | 8 |

That is more than a hand-count of the source tree suggests, because the scan
deliberately includes tests, fixtures and migrations. A new test that names
somebody's agent is a smaller leak than a new UI screen, but it is the same
habit — and the habit is what put the fleet compose file in the repo.

## How it is enforced

`scripts/privacy-scan.py` runs in CI (the `leak-gate` job, next to gitleaks).
It scans every tracked file for the fleet-name blocklist and compares the
result with the baseline below:

| situation | result |
|---|---|
| a name appears in a file the baseline does not list | ❌ CI fails — this is a **new** leak |
| a name disappears from a listed file | ❌ CI fails, asking you to strike the line |
| everything matches | ✅ green |

Both directions fail on purpose. The first stops new introductions; the second
keeps this page honest, so it shrinks as the work gets done instead of
quietly describing a state that no longer exists.

The address shapes — absolute `/Users/<name>` paths, the Tailscale range
`100.64.0.0/10`, `<host>.<tailnet>.ts.net` — are handled by `.gitleaks.toml`
in the same CI job, with **zero tolerance**: the tree is already clean under
those rules, so anything found there is new by definition.

## Working the backlog

1. Replace the name with a neutral one (`alpha`, `beta`, `<slug>`, or a role
   word like `reviewer`) — or delete the passage if it only ever described
   somebody's setup.
2. Run `scripts/privacy-scan.py --update-baseline`.
3. Commit both changes together.

Rewriting history is **not** part of this: the names are in old commits and
will stay there. This is about what a fresh clone shows.

### Where to start

The list is ordered by how visible the leak is, not by how easy it is:

1. **Product surface** — anything a user of *their own* installation would
   see. `frontend-v2/src/components/pages/OfficeView/OrgChart/org-chart-data.ts`
   was the worst of these (a full org chart of the author's fleet, live at
   `/office`) and is already done.
2. **Docs people read to get started** — `docs/ARCHITECTURE.md`,
   `docs/agent-configuration-standard.md`, `docs/setup/*`.
3. **Code comments and defaults** — `backend/app/**`, `scripts/**`. Usually a
   sentence like "Sparky runs the omp bridge" that should name the *runtime*,
   not the agent.
4. **ADRs and migrations** — the lowest priority on purpose. They are dated
   records of decisions that really were made about those agents; rewriting
   them costs accuracy. Where a name is load-bearing for the history, leave
   it and say so here.

## Baseline

Machine-readable. `path: name, name` — one line per file. Do not edit by hand;
use `scripts/privacy-scan.py --update-baseline`.

<!-- privacy-baseline:begin -->
```
PRODUCT.md: freecode
backend/alembic/versions/0069_requires_git_workflow.py: davinci, shakespeare
backend/alembic/versions/0078_link_sparky_runtime.py: sparky
backend/alembic/versions/0079_link_cloud_agents.py: sparky
backend/alembic/versions/0081_claude_fleet_runtime_binding.py: davinci, freecode, shakespeare, sparky
backend/alembic/versions/0082_deprecate_checkpoint_comments.py: sparky
backend/alembic/versions/0085_seed_agent_personas.py: davinci, freecode, shakespeare, sparky
backend/alembic/versions/0086_remove_neo_and_planner.py: freecode, sparky
backend/alembic/versions/0092_agent_auto_promote_on_resolution.py: davinci, freecode, shakespeare, sparky
backend/alembic/versions/0093_agent_runtime_cleanup_docker_to_cli_bridge.py: davinci, freecode, sparky
backend/alembic/versions/0095_hermes_runtime_and_single_instance.py: sparky
backend/alembic/versions/0097_per_agent_idle_timeout_minutes.py: davinci, freecode
backend/alembic/versions/0106_storyboards.py: davinci
backend/alembic/versions/0107_storyboard_review_loop.py: davinci, shakespeare
backend/alembic/versions/0108_video_performance.py: shakespeare
backend/alembic/versions/0112_vault_cutover.py: sparky
backend/alembic/versions/0125_drop_agents_heartbeat_md.py: sparky
backend/alembic/versions/0151_agent_use_operating_card.py: sparky
backend/alembic/versions/0168_merge_lmstudio_rows.py: sparky
backend/app/auth.py: davinci
backend/app/config.py: freecode, sparky
backend/app/models/agent.py: sparky
backend/app/models/model_usage.py: sparky
backend/app/models/storyboard.py: davinci
backend/app/models/trend.py: shakespeare
backend/app/models/video_performance.py: shakespeare
backend/app/routers/agent_chat.py: davinci
backend/app/routers/agent_comments.py: sparky
backend/app/routers/agent_scoped.py: freecode
backend/app/routers/agent_task_status.py: davinci, sparky
backend/app/routers/agents.py: sparky
backend/app/routers/cli_terminal.py: freecode, sparky
backend/app/routers/internal.py: davinci
backend/app/routers/tasks.py: freecode
backend/app/routers/vault.py: sparky
backend/app/services/chat_inbound.py: freecode
backend/app/services/chat_slack.py: freecode
backend/app/services/cli_bridge_runner.py: freecode, sparky
backend/app/services/deliverable_paths.py: freecode
backend/app/services/dispatch.py: freecode
backend/app/services/docker_agent_sync.py: freecode, shakespeare, sparky
backend/app/services/operations.py: sparky
backend/app/services/pdf_generator.py: freecode
backend/app/services/plugin_manager.py: shakespeare, sparky
backend/app/services/provisioning.py: freecode
backend/app/services/runtime_model_resolver.py: sparky
backend/app/services/task_context_builder.py: freecode
backend/app/services/task_lifecycle.py: davinci
backend/app/services/task_runner.py: freecode, sparky
backend/app/services/template_renderer.py: davinci, freecode, shakespeare, sparky
backend/app/services/template_seeder.py: sparky
backend/app/services/token_harvester.py: sparky
backend/app/services/tools_md_builder.py: davinci, freecode, shakespeare, sparky
backend/app/services/transcript_chat.py: davinci
backend/app/services/work_context.py: freecode
backend/app/verticals/bench_studio/orchestrator.py: sparky
backend/config/runtimes.json: sparky
backend/scripts/create_free_code_agent.py: freecode
backend/templates/SOUL.md.j2: davinci, freecode, shakespeare, sparky
backend/templates/docs/pdf-office.md.j2: freecode
backend/tests/context_scrape_fixtures.py: sparky
backend/tests/fixtures/panes/claude/meta.json: freecode
backend/tests/fixtures/panes/openclaude/meta.json: freecode, shakespeare
backend/tests/test_agent_chat_input.py: freecode
backend/tests/test_agent_image_no_provider_defaults.py: shakespeare
backend/tests/test_agent_restart_never_targets_runtime_container.py: sparky
backend/tests/test_approval_install_hook.py: davinci
backend/tests/test_bench_orchestrator_flow.py: sparky
backend/tests/test_blocked_at_dedicated_timestamp.py: sparky
backend/tests/test_blocker_guard_precommit.py: sparky
backend/tests/test_bootstrap_auth.py: freecode
backend/tests/test_bootstrap_gh_token.py: freecode
backend/tests/test_bootstrap_recovery.py: freecode
backend/tests/test_boss_orchestrator.py: sparky
backend/tests/test_card_inbox_reply_rule.py: freecode, shakespeare, sparky
backend/tests/test_card_verbs_by_scope.py: sparky
backend/tests/test_chat_adapter_tck.py: sparky
backend/tests/test_chat_slack.py: freecode, sparky
backend/tests/test_clarification_callback_runtime_agnostic.py: sparky
backend/tests/test_cli_bridge_runner.py: freecode
backend/tests/test_cli_bridge_session_name.py: freecode
backend/tests/test_cli_terminal.py: freecode
backend/tests/test_cli_tools_router.py: sparky
backend/tests/test_comment_delivery_via_poll.py: davinci, sparky
backend/tests/test_compose_renderer.py: davinci, sparky
backend/tests/test_compose_renderer_excludes_host_agents.py: davinci, sparky
backend/tests/test_compose_renderer_new_agents.py: sparky
backend/tests/test_compose_renderer_omp_sessions.py: sparky
backend/tests/test_compose_renderer_prune.py: sparky
backend/tests/test_compose_renderer_token_hardening.py: sparky
backend/tests/test_context_detect.sh: sparky
backend/tests/test_d1_dispatch_attempt_rotation.py: sparky
backend/tests/test_d2_dispatch_escalation_telegram.py: sparky
backend/tests/test_davinci_feedback_fixes.py: davinci
backend/tests/test_delegation_contracts.py: sparky
backend/tests/test_deliverable_path_validator.py: freecode
backend/tests/test_deliverables_validation_and_memory.py: davinci, freecode
backend/tests/test_dispatch_size_log_dedup.py: sparky
backend/tests/test_docker_agent_sync_runtime.py: davinci, freecode, sparky
backend/tests/test_drift_watchdog.py: sparky
backend/tests/test_harness_catalog.py: freecode
backend/tests/test_heartbeat_status_sync.py: davinci, sparky
backend/tests/test_henry_sunset_acceptance.py: sparky
backend/tests/test_hermes_dispatch_config.py: davinci, freecode
backend/tests/test_idle_threshold_by_role.py: davinci
backend/tests/test_inbox_pull.py: sparky
backend/tests/test_jarvis_core_tools.py: sparky
backend/tests/test_local_memory_and_recreate.py: sparky
backend/tests/test_message_delivery.py: sparky
backend/tests/test_migration_0168_lmstudio_merge.py: sparky
backend/tests/test_omp_runtime.py: sparky
backend/tests/test_operating_card.py: freecode, shakespeare
backend/tests/test_orchestrator_dispatch_dependencies.py: davinci, shakespeare
backend/tests/test_pane_state.py: freecode
backend/tests/test_parent_reopen_on_subtask.py: davinci
backend/tests/test_phase1_integrity.py: sparky
backend/tests/test_phase2a_observability.py: sparky
backend/tests/test_poll_blocked_grace_window.py: sparky
backend/tests/test_provisioning_workspace_path.py: freecode
backend/tests/test_recreate_propagation.py: sparky
backend/tests/test_requires_git_workflow_flag.py: davinci, freecode
backend/tests/test_review_policy.py: sparky
backend/tests/test_runtime_context_drift.py: sparky
backend/tests/test_runtime_env_hermes.py: sparky
backend/tests/test_runtime_propagation.py: sparky
backend/tests/test_session_monitor_skips_non_gateway.py: freecode
backend/tests/test_skill_sync_to_disk.py: shakespeare, sparky
backend/tests/test_slack_inbound.py: freecode
backend/tests/test_soul_chat_origin_reply_rule.py: freecode
backend/tests/test_soul_inbox_reply_rule.py: freecode, shakespeare
backend/tests/test_soul_template_hermes_mention.py: sparky
backend/tests/test_stale_check_user_resolution.py: sparky
backend/tests/test_stop_resume_preserves_assignment.py: sparky
backend/tests/test_stuck_in_progress_watchdog.py: sparky
backend/tests/test_tailscale_endpoint.py: sparky
backend/tests/test_task_runner_docker_cli_bridge.py: davinci
backend/tests/test_task_runner_session_name.py: freecode
backend/tests/test_template_renderer_roles.py: davinci, freecode, shakespeare
backend/tests/test_token_harvester.py: freecode, sparky
backend/tests/test_tools_md_project.py: freecode
backend/tests/test_transcript_chat_parser.py: davinci
backend/tests/test_unblock_liveness_redispatch.py: sparky
backend/tests/test_unblock_race_two_in_progress.py: sparky
backend/tests/test_vault_activity.py: sparky
backend/tests/test_vault_briefing.py: sparky
backend/tests/test_vault_contradiction.py: sparky
backend/tests/test_vault_embeddings.py: sparky
backend/tests/test_vault_embeddings_dgx.py: sparky
backend/tests/test_vault_frontmatter.py: sparky
backend/tests/test_vault_git.py: sparky
backend/tests/test_vault_git_live.py: sparky
backend/tests/test_vault_graph.py: sparky
backend/tests/test_vault_index.py: sparky
backend/tests/test_vault_lint.py: sparky
backend/tests/test_vault_migration.py: davinci, sparky
backend/tests/test_vault_routes.py: sparky
backend/tests/test_vault_routes_agent_search.py: sparky
backend/tests/test_vault_routes_write.py: sparky
backend/tests/test_vault_status_filter.py: sparky
backend/tests/test_vault_stream.py: sparky
backend/tests/test_vault_watcher.py: sparky
backend/tests/test_vault_watcher_moved.py: sparky
backend/tests/test_voice_graph_highlight.py: sparky
backend/tests/test_voice_worker_mc_client.py: davinci, sparky
backend/tests/test_watchdog_dedup_and_renewal.py: sparky
backend/tests/test_workspace_no_silent_fallback.py: davinci, freecode
backend/tests/test_workspace_path_validation.py: freecode
docker-compose.override.example.yml: freecode
docker/mc-agent-base/Dockerfile: freecode
docker/mc-agent-base/lib/context-detect.sh: davinci, shakespeare, sparky
docker/mc-agent-base/lib/mc-pre-push.sh: freecode
docker/mc-agent-base/lib/paste-verify.sh: sparky
docker/mc-agent-base/lib/ui-detect.sh: sparky
docker/mc-agent-base/recycler.sh: sparky
docker/mc-claude-agent/Dockerfile: davinci, sparky
docker/mc-claude-agent/lib/context-detect.sh: davinci, shakespeare, sparky
docker/mc-claude-agent/lib/mc-pre-push.sh: freecode
docker/mc-claude-agent/lib/paste-verify.sh: sparky
docker/mc-claude-agent/lib/ui-detect.sh: sparky
docker/mc-claude-agent/recycler.sh: sparky
docker/mc-kimi-agent/lib/context-detect.sh: davinci, shakespeare, sparky
docker/mc-kimi-agent/lib/mc-pre-push.sh: freecode
docker/mc-kimi-agent/lib/paste-verify.sh: sparky
docker/mc-kimi-agent/lib/ui-detect.sh: sparky
docker/mc-playwright/service.py: freecode, sparky
docker/omp-bridge/README.md: sparky
docker/omp-bridge/bridge.py: sparky
docker/omp-bridge/context_detect.py: shakespeare, sparky
docker/omp-bridge/tests/test_heartbeat_context.py: sparky
docker/omp-bridge/tests/test_inject_retry.py: sparky
docker/omp-bridge/tests/test_native_tui.py: sparky
docker/omp-bridge/tests/test_serve_loop.py: sparky
docker/shared/poll.sh: freecode, sparky
docs/ARCHITECTURE.md: davinci, freecode, shakespeare, sparky
docs/agent-configuration-standard.md: davinci, freecode, shakespeare, sparky
docs/assets/how-it-works.svg: sparky
docs/decisions/005-board-lead-first-dispatch.md: freecode
docs/decisions/017-runtime-registry-db.md: sparky
docs/decisions/019-claude-fleet-hybrid.md: davinci, freecode, shakespeare, sparky
docs/decisions/020-harness-phase2-mc-cli.md: sparky
docs/decisions/021-agent-personas.md: davinci, freecode, shakespeare, sparky
docs/decisions/022-mc-home-workspace-layout.md: davinci, freecode, shakespeare, sparky
docs/decisions/023-review-policy-trust-by-default.md: freecode, sparky
docs/decisions/024-claude-process-recycling.md: davinci, freecode, shakespeare, sparky
docs/decisions/027-universal-agent-runtime-binding.md: davinci
docs/decisions/028-runtime-registry-and-session-propagation.md: sparky
docs/decisions/029-hermes-host-side-tmux-worker.md: sparky
docs/decisions/031-hermes-hardening-poll-claim-and-host-path-and-idle-timeout.md: davinci, freecode, shakespeare
docs/decisions/033-secrets-vs-credentials-boundary.md: sparky
docs/decisions/039-openclaw-gateway-sunset.md: davinci, sparky
docs/decisions/045-omp-runtime.md: sparky
docs/decisions/046-lifecycle-safety-watchdog.md: sparky
docs/decisions/047-docker-socket-proxy.md: sparky
docs/decisions/049-omp-native-tui-session.md: sparky
docs/decisions/056-harness-provider-decoupling.md: sparky
docs/decisions/073-sessions-chat-transcript-tailing.md: sparky
docs/decisions/README.md: sparky
docs/lifecycle-safety-watchdog-REPORT.md: sparky
docs/omp-runtime-REPORT.md: sparky
docs/setup/slack.md: freecode
frontend-v2/src/app/insights/page.tsx: sparky
frontend-v2/src/app/runtimes/__tests__/cloud-usage.test.tsx: davinci, freecode, shakespeare, sparky
frontend-v2/src/components/chat/SessionSidebar.test.tsx: sparky
frontend-v2/src/components/files/__tests__/FilesSearchFilters.test.tsx: sparky
frontend-v2/src/components/memory/NoteSidePanel.tsx: sparky
frontend-v2/src/components/memory/VaultTopicsView.tsx: davinci, freecode, sparky
frontend-v2/src/components/shared/__tests__/CliToolsSection.test.tsx: sparky
frontend-v2/src/components/shared/__tests__/RuntimePill.test.tsx: davinci
frontend-v2/src/components/shared/__tests__/RuntimeSwitchModal.test.tsx: davinci
frontend-v2/src/components/vault/VaultReadingPanel.tsx: sparky
frontend-v2/src/verticals/bench_studio/__tests__/NewChallengeDialog.test.tsx: sparky
jarvis_core/mc_client.py: sparky
jarvis_core/persona.py: davinci, freecode, shakespeare, sparky
jarvis_core/tools.py: sparky
scripts/auto-rebuild-agents-on-drift.sh: sparky
scripts/build-agent-images.sh: sparky
scripts/cli-bridge.py: freecode
scripts/context_detect.py: shakespeare, sparky
scripts/free-code-bridge.py: freecode
scripts/init-mc-deliverables-dirs.sh: davinci, freecode, shakespeare, sparky
scripts/mc-cli/mc_cli/commands.py: davinci, sparky
scripts/mc-cli/mc_cli/config.py: davinci
scripts/mc-cli/tests/test_config_priority.py: davinci
scripts/mc-cli/tests/test_finish_preflight.py: sparky
tools/generate-agent-map.py: freecode, sparky
tools/record-pane-fixtures.sh: freecode
voice_worker/main.py: sparky
```
<!-- privacy-baseline:end -->
