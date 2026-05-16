"""
Plugin registration shim for Feishu Custom platform adapter.

Uses ``FEISHU_CUSTOM_APP_ID`` / ``FEISHU_CUSTOM_APP_SECRET`` env vars
so the built-in ``feishu`` adapter is never auto-enabled.

Also monkey-patches ``run_agent.AIAgent`` to capture the actual
model/provider used (after fallback) so the CardKit footer can
display ``<provider>/<model>``.
"""


def register(ctx):
    """Register the Feishu Custom adapter with the platform registry."""
    from gateway.platforms.feishu_custom import FEISHU_CUSTOM_ENTRY
    _install_model_tracker()
    ctx.register_platform(**FEISHU_CUSTOM_ENTRY)


# ── Model tracking monkey-patch ──────────────────────────────────────
_TRACKER_INSTALLED = False


def _install_model_tracker() -> None:
    global _TRACKER_INSTALLED
    if _TRACKER_INSTALLED:
        return
    _TRACKER_INSTALLED = True

    try:
        import run_agent
        from run_agent import AIAgent
        import gateway.platforms.feishu_custom as fcm

        # ── run_conversation ──────────────────────────────────────────
        _original_run = AIAgent.run_conversation

        def _tracking_run_conversation(self, *args, **kwargs):
            try:
                fcm._last_model_info["model"] = self.model
                fcm._last_model_info["provider"] = self.provider or ""
            except Exception:
                pass
            result = _original_run(self, *args, **kwargs)
            try:
                fcm._last_model_info["model"] = self.model
                fcm._last_model_info["provider"] = self.provider or ""
            except Exception:
                pass
            return result

        AIAgent.run_conversation = _tracking_run_conversation

        # ── _try_activate_fallback ────────────────────────────────────
        # Runtime fallback updates self.model / self.provider mid-turn.
        # Sync _last_model_info immediately so the footer shows the
        # actual model used, not the pre-fallback one.
        if hasattr(AIAgent, "_try_activate_fallback"):
            _original_fallback = AIAgent._try_activate_fallback

            def _tracking_fallback(self, *args, **kwargs):
                result = _original_fallback(self, *args, **kwargs)
                try:
                    fcm._last_model_info["model"] = self.model
                    fcm._last_model_info["provider"] = self.provider or ""
                except Exception:
                    pass
                return result

            AIAgent._try_activate_fallback = _tracking_fallback

    except ImportError:
        pass
