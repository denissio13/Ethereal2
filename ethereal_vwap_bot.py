
    
        try:
            sender = getattr(getattr(self.client, "chain", None), "address", None)
            order_type = "MARKET" if self.cfg.exits_as_stop_market else "LIMIT"
            expires_at = int(time.time()) + int(self.cfg.exit_expires_in_sec)
            tp = await self.client.create_order(
                order_type=order_type,
                product_id=self.product_id,
                ticker=self.cfg.ticker,
                side=exit_side,
                quantity=float(q),
                     if orders is None:
            try:
                orders = await self.client.list_orders(
                    subaccount_id=str(self.subaccount_id),
                    product_ids=[str(self.product_id)],
                    limit=200,
                    order="desc",
                    order_by="createdAt",
                )
            except Exception:
                return None
        if not orders:
            return None

        want_group = str(self.state.get("exit_group_id") or "")
        for o in orders:
            try:
                if not self._order_is_filled(o):
                    continue
                # match group if we have it (preferred)
                og = str(getattr(o, "group_id", "") or getattr(o, "groupId", "") or "")
                if want_group and og and og != want_group:
                    continue
                reduce_only = bool(
                    getattr(o, "reduce_only", False)
                    or getattr(o, "reduceOnly", False)
                    or getattr(o, "close", False)
                    or getattr(o, "isClose", False)
                )
                if not reduce_only:
                    continue
                oid = str(getattr(o, "id", "") or "")
                r = self._infer_exit_reason_from_order(oid=oid, o=o)
                if r in {"SL", "TP"}:
                    return r
            except Exception:
                continue
        return None

    @staticmethod
    def _to_ms_ts(v: Any) -> int:
        """
        Best-effort timestamp -> milliseconds since epoch.
        Supports: int/float (sec or ms), numeric strings, ISO-8601 strings, datetime.
        """
        try:
            if v is None:
                return 0
            if isinstance(v, datetime):
                return _dt_to_ms(v if v.tzinfo else v.replace(tzinfo=timezone.utc))
            if isinstance(v, (int, float)):
                iv = int(v)
                if iv <= 0:
                    return 0
                # heuristic: seconds vs ms
                return iv * 1000 if iv < 10_000_000_000 else iv
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return 0
                # numeric string?
                try:
                    iv = int(s)
                    return iv * 1000 if iv < 10_000_000_000 else iv
                except Exception:
                    pass
                try:
                    fv = float(s)
                    iv = int(fv)
                    return iv * 1000 if iv < 10_000_000_000 else iv
                except Exception:
                    pass
                # ISO-8601 string?
                try:
                    ss = s.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ss)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return _dt_to_ms(dt.astimezone(timezone.utc))
                except Exception:
                    return 0
        except Exception:
            return 0
        return 0

    @staticmethod
    def _stop_type_int(st_obj: Any) -> Optional[int]:
        """
        Normalize stop_type to int where possible.
        Returns 0 (TP/GAIN), 1 (SL/LOSS) or None.
        """
        try:
            stv = getattr(st_obj, "value", st_obj)
            try:
                stv_int = int(stv)
                return stv_int if stv_int in {0, 1} else None
            except Exception:
                pass
            s = str(stv).strip().lower()
            if not s:
                return None
            # tolerate enum/string variants
            if "loss" in s or s == "sl" or s.endswith("_loss"):
                return 1
            if "gain" in s or s == "tp" or s.endswith("_gain"):
                return 0
            return None
        except Exception:
            return None

    @classmethod
    def _order_ts_ms(cls, o: Any) -> int:
        """
        Best-effort order timestamp (ms).
        Prefer filledAt/updatedAt/createdAt if present.
        """
        for k in ("filledAt", "filled_at", "updatedAt", "updated_at", "createdAt", "created_at"):
            try:
                v = getattr(o, k, None)
                if v is None:
                    continue
                iv = cls._to_ms_ts(v)
                if iv > 0:
                    return iv
            except Exception:
                continue
        return 0

    async def _detect_recent_reduce_only_stop_fill(self) -> Optional[tuple[str, str, int]]:
        """
        Scan recent orders and return (reason, order_id, ts_ms) for the latest *new* FILLED reduce-only stop order.
        reason: 'SL' if stop_type==1, 'TP' if stop_type==0
        """
        if not self.subaccount_id or not self.product_id:
            return None
        last_ts = int(self.state.get("last_handled_reduce_stop_ts") or 0)
        last_id = str(self.state.get("last_handled_reduce_stop_order_id") or "")
        want_group = str(self.state.get("exit_group_id") or "")
        placed_ms = int(self.state.get("exit_placed_ms") or 0)

        orders = None
        try:
            orders = await self.client.list_orders(
                subaccount_id=str(self.subaccount_id),
                product_ids=[str(self.product_id)],
                limit=200,
                order="desc",
                order_by="updatedAt",
            )
        except Exception:
            orders = None
        if orders is None:
            try:
                orders = await self.client.list_orders(
                    subaccount_id=str(self.subaccount_id),
                    product_ids=[str(self.product_id)],
                    limit=200,
                    order="desc",
                    order_by="createdAt",
                )
            except Exception:
                return None
        if not orders:
            return None

        best: Optional[tuple[str, str, int]] = None
        for o in orders:
            try:
                if not self._order_is_filled(o):
                    continue
                reduce_only = bool(
                    getattr(o, "reduce_only", False)
                    or getattr(o, "reduceOnly", False)
                    or getattr(o, "close", False)
                    or getattr(o, "isClose", False)
                )
                if not reduce_only:
                    continue
                # If we have an OCO group id, prefer matching it to avoid false positives.
                og = str(getattr(o, "group_id", "") or getattr(o, "groupId", "") or "")
                if want_group and og and og != want_group:
                    continue
                st = getattr(o, "stop_type", None)
                stv_int = self._stop_type_int(st)
                if stv_int not in {0, 1}:
                    continue

                oid = str(getattr(o, "id", "") or "")
                ts = self._order_ts_ms(o)
                if not oid or ts <= 0:
                    continue
                # If we know when exits were placed, ignore fills that predate it (avoid old historical order match).
                if placed_ms and ts < (placed_ms - 60_000):
                    continue
                # Dedup: ignore already handled
                if oid == last_id:
                    continue
                if ts <= last_ts:
                    continue

                reason = "SL" if stv_int == 1 else "TP"
                cand = (reason, oid, ts)
                if best is None or ts > best[2]:
                    best = cand
            except Exception:
                continue

        return best

    async def _check_and_handle_exit_fills(
        self,
        vwap: Decimal,
        entry_px: Decimal,
        tp_px: Decimal,
        sl_px: Decimal,
        *,
        trade_direction: Optional[str] = None,
    ) -> bool:
        """
        Check current OCO exit orders recorded in state and handle a fill immediately.

        This is more reliable than position-based detection because a position can open+close
        between polling intervals, while the FILLED order remains queryable.

        Returns True if we handled a fill (and caller should stop further actions this step).
        """
        exit_ids = list(self.state.get("exit_order_ids") or [])
        if not exit_ids:
            return False

        reason = await self._detect_close_reason(exit_ids)
        if not reason:
            # Fallback: handle cases where OCO legs become 404 or state mapping drifted.
            try:
                reason = await self._detect_close_reason_from_recent_orders()
            except Exception:
                reason = None
        if not reason:
            # Last resort: recent reduce-only stop scan (filtered by group/placed_ms inside).
            try:
                recent = await self._detect_recent_reduce_only_stop_fill()
                if recent:
                    reason = str(recent[0] or "")
            except Exception:
                reason = None
        if not reason:
            # If both exits are no longer working (stale ids), clear them to avoid blocking entries forever.
            try:
                statuses = [await self._confirm_order_status(str(oid)) for oid in exit_ids]
                if statuses and all(self._is_terminal_status(s) and (s or "").upper() != "FILLED" for s in statuses):
                    await self._cancel_exits()
                    self._save_state()
            except Exception:
                pass
            return False

        # Exit filled: cancel leftovers and record close reason.
        await self._cancel_exits()
        await self._cancel_entry()
        self.state["last_close_reason"] = reason
        self.state["last_close_at"] = _utc_now().isoformat()

        # Capture context for pause logs before we potentially clear anchor_open state.
        pause_td = self._pause_trade_direction(trade_direction)
        pause_strategy = self._cfg_strategy()

        # If anchor_open strategy trade was active, mark it closed.
        if self.state.get("anchor_open_active"):
            self._clear_anchor_open_state(reason=reason)
        self._clear_vwap_trade_direction()

        # Update stats (best-effort) using exit trigger prices.
        if reason == "SL":
            pause_min = self._pause_after_sl_minutes()
            if pause_min > 0:
                if self._should_ignore_sl_pause():
                    logger.info(
                        "SL pause ignored until first open: ticker=%s strategy=%s dir=%s source=exit_fill",
                        self.cfg.ticker,
                        pause_strategy,
                        pause_td,
                    )
                else:
                    await self._cancel_entry()
                    self._set_pause_after_sl(
                        pause_min,
                        vwap=vwap,
                        entry_px=entry_px,
                        tp_px=tp_px,
                        sl_px=sl_px,
                        source="exit_fill",
                        trade_direction=pause_td,
                        strategy=pause_strategy,
                    )
                    return True

        self._save_state()
        logger.info(
            "Position closed by %s (from exit order fill). vwap=%s entry=%s tp=%s sl=%s",
            reason,
            str(vwap),
            str(entry_px),
            str(tp_px),
            str(sl_px),
        )
        return False

    async def _handle_no_position_close_and_pause(
        self,
        *,
        prev_pos_size: Decimal,
        pos_size: Decimal,
        vwap: Decimal,
        entry_px: Decimal,
        tp_px: Decimal,
        sl_px: Decimal,
        trade_direction: Optional[str] = None,
    ) -> bool:
        """
        Common (all strategies) close detection when position is not visible.
        Goal: reliably detect SL/TP, set pause on SL, and clear stale exit state without risking unprotecting a live position.
        Returns True if caller should stop further actions this step.
        """
        # If we have neither a close transition nor any exit context, do nothing.
        exit_ids = list(self.state.get("exit_order_ids") or [])
        has_exit_ctx = bool(exit_ids or (self.state.get("exit_group_id") or "") or int(self.state.get("exit_placed_ms") or 0))
        closed_transition = _is_pos_finite_decimal(prev_pos_size) and prev_pos_size > 0 and (not _is_pos_finite_decimal(pos_size) or pos_size <= 0)

        recent_reason: Optional[str] = None
        recent_oid: Optional[str] = None
        recent_ts: Optional[int] = None
        # Avoid false positives on startup when we have no exit context and no close transition.
        if has_exit_ctx or closed_transition:
            try:
                recent = await self._detect_recent_reduce_only_stop_fill()
                if recent:
                    recent_reason, recent_oid, recent_ts = str(recent[0]), str(recent[1]), int(recent[2])
            except Exception:
                recent_reason = None

        # If we have a recent reduce-only stop fill (for our group/placed window), treat as a close event.
        if recent_reason in {"SL", "TP"}:
            # Capture context before clearing anchor state
            pause_td = self._pause_trade_direction(trade_direction)
            pause_strategy = self._cfg_strategy()
            if recent_oid and recent_ts:
                self.state["last_handled_reduce_stop_order_id"] = recent_oid
                self.state["last_handled_reduce_stop_ts"] = int(recent_ts)
                self.state["last_close_reason"] = recent_reason
                self.state["last_close_at"] = _ms_to_dt(int(recent_ts)).isoformat()
            # Clear exits/entry and pause if needed.
            await self._cancel_exits()
            await self._cancel_entry()
            if self.state.get("anchor_open_active"):
                self._clear_anchor_open_state(reason=recent_reason)
            self._clear_vwap_trade_direction()
            self._save_state()
            if recent_reason == "SL":
                pause_min = self._pause_after_sl_minutes()
                if pause_min > 0:
                    if self._should_ignore_sl_pause():
                        logger.info(
                            "SL pause ignored until first open: ticker=%s strategy=%s dir=%s source=recent_stop_order",
                            self.cfg.ticker,
                            pause_strategy,
                            pause_td,
                        )
                    else:
                        self._set_pause_after_sl(
                            pause_min,
                            vwap=vwap,
       
        d = (direction or "").strip().upper()
        exit_side = 1 if d == "LONG" else 0
        oid = await self._place_market_order(side=exit_side, qty=qty, reduce_only=True, tag="X")
        if oid:

        # TP and SL based on open price vs previous anchor VWAP.
        tp_price = prev_vwap
        tp_size = (open_px - prev_vwap).copy_abs()
        if tp_size <= 0:
            self.state["anchor_open_decision_anchor_ms"] = int(anchor_start_ms)
            self._save_state()
            return

        sl_pct_of_tp = _as_decimal(getattr(self.cfg, "anchor_open_sl_pct_of_tp", Decimal("100")) or "100")
        if sl_pct_of_tp < 0:
            sl_pct_of_tp = Decimal("0")
        sl_size = tp_size * (sl_pct_of_tp / Decimal("100"))
        # Stop is placed around the entry reference (open price).
        sl_price = (open_px - sl_size) if trade_dir == "LONG" else (open_px + sl_size)

        qty = self._round_qty(self.cfg.entry_quantity)
        if qty <= 0:
            self.state["anchor_open_decision_anchor_ms"] = int(anchor_start_ms)
            self._save_state()
            logger.error("ANCHOR_OPEN: qty<=0; check entry_quantity")
            return

        oid = await self._place_market_order(side=side, qty=qty, reduce_only=False, tag="E")
        if not oid:
            return

        self.state["anchor_open_active"] = True
        self.state["anchor_open_direction"] = trade_dir
        self.state["anchor_open_tp_price"] = str(tp_price)
        self.state["anchor_open_sl_price"] = str(sl_price)
        self.state["anchor_open_tp_size"] = str(tp_size)
        self.state["anchor_open_delta_pct"] = str(delta_pct)
        self.state["anchor_open_opened_at"] = now.isoformat()
        self.state["anchor_open_decision_anchor_ms"] = int(anchor_start_ms)
        self._save_state()

        # Immediate protection: place TP/SL using configured entry qty (do not wait for position visibility).
        # This prevents anchor_open entries from being unprotected when the position endpoint lags.
        await self._place_oco_exits_for_qty(
            qty=qty,
            tp_px=tp_price,
            sl_px=sl_price,
            direction=trade_dir,
        )

        logger.warning(
            "ANCHOR_OPEN ENTRY: %s %s qty=%s (order_id=%s) prev_vwap=%s open=%s delta_pct=%s thr=%s TP=%s SL=%s sl_pct_of_tp=%s tp_size=%s",
            trade_dir,
            self.cfg.ticker,
            qty,
            oid,
            str(prev_vwap),
            str(open_px),
            str(delta_pct),
            str(thr),
            str(self._round_price(tp_price)),
            str(self._round_price(sl_price)),
            str(sl_pct_of_tp),
            str(tp_size),
        )


    def _pause_after_sl_minutes(self) -> int:
        """
        Returns pause duration in minutes after SL.

        New model: cfg.pause_after_sl_minutes (preferred).
        Backward compatibility: if cfg.pause_on_sl is true and pause_after_sl_minutes<=0 -> default to 60 minutes.
        """
        try:
            m = int(getattr(self.cfg, "pause_after_sl_minutes", 0) or 0)
        except Exception:
            m = 0
        if m > 0:
            return m
        if bool(getattr(self.cfg, "pause_on_sl", False)):
            return 60
        return 0

    def _tightened_sl(
        self,
        *,
        current_sl: Optional[Decimal],
        desired_sl: Decimal,
        trade_direction: Optional[str] = None,
    ) -> Decimal:
        """
        VWAP strategy rule: after entry, SL can only tighten (reduce risk).
        - LONG: SL price can only move up (increase numerically).
        - SHORT: SL price can only move down (decrease numerically).
        """
        if current_sl is None or not _is_pos_finite_decimal(current_sl):
            return desired_sl
        if not _is_pos_finite_decimal(desired_sl):
            return current_sl
        d = self._normalize_direction_value(trade_direction or self.direction)
        if d not in {"LONG", "SHORT"}:
            return current_sl
        if d == "LONG":
            return desired_sl if desired_sl > current_sl else current_sl
        # SHORT
        return desired_sl if desired_sl < current_sl else current_sl

    def _trailing_sl_candidate(self, *, trade_direction: str, current_sl: Optional[Decimal]) -> Optional[Decimal]:
        """
        Compute trailing SL candidate from the previous candle body, according to the requested rule.
        Uses prev_candle_open_price/prev_candle_close_price tracked from reference price.
        """
        if current_sl is None or not _is_pos_finite_decimal(current_sl):
            return None
        if not bool(getattr(self.cfg, "trailing_stop_enabled", False)):
            return None
        mode = str(getattr(self.cfg, "trailing_candle_filter", "all") or "all").strip().lower()
        if mode not in {"all", "opposite"}:
            mode = "all"

        d = (trade_direction or "").strip().upper()
        if d not in {"LONG", "SHORT"}:
            return None

        try:
            o = _as_decimal(self.state.get("prev_candle_open_price"))
            c = _as_decimal(self.state.get("prev_candle_close_price"))
        except Exception:
            return None
        if not _is_pos_finite_decimal(o) or not _is_pos_finite_decimal(c) or o == c:
            return None

        # "all" mode: always tighten by abs body.
        if mode == "all":
            body = (c - o).copy_abs()
            if body <= 0:
                return None
            return (current_sl + body) if d == "LONG" else (current_sl - body)

        # "opposite" mode (original rule):
        # LONG: move SL up only if prev candle bearish (close < open)
        if d == "LONG" and c < o:
            body = o - c
            return current_sl + body
        # SHORT: move SL down only if prev candle bullish (close > open)
        if d == "SHORT" and c > o:
            body = c - o
            return current_sl - body
        return None

    def _trailing_debug(self, msg: str, *args: object) -> None:
        if bool(getattr(self.cfg, "trailing_debug_logs", False)):
            logger.info(msg, *args)

    def _bybit_interval_for_timeframe(self) -> Optional[str]:
        """
        Map bot timeframe (e.g. 1m/5m/1h/4h/1d) to Bybit kline interval.
        Returns interval string accepted by Bybit v5.
        """
        tf = (self.cfg.timeframe or "").strip().lower()
        if not tf:
            return None
        num = ""
        unit = ""
        for ch in tf:
            if ch.isdigit():
                num += ch
            else:
                unit += ch
        if not num or not unit:
            return None
        n = int(num)
        if unit == "m":
            return str(n)
        if unit == "h":
            return str(n * 60)
        if unit == "d":
            return "D" if n == 1 else None
        return None

    async def _bybit_prev_candle_ohlc(self, prev_start_ms: int, tf_ms: int) -> Optional[tuple[Decimal, Decimal, Decimal, Decimal]]:
        """
        Fetch previous candle open/close from Bybit kline API.
        Uses cfg.bybit_* settings.
        """
        interval = self._bybit_interval_for_timeframe()
        if not interval:
            return None
        base_url = (self.cfg.bybit_base_url or "https://api.bybit.com").rstrip("/")
        url = f"{base_url}/v5/market/kline"
        category = str(self.cfg.bybit_category or "linear")
        symbol = str(self.cfg.bybit_symbol or "")
        if not symbol:
            return None
        start = int(prev_start_ms)
        end = int(prev_start_ms + tf_ms - 1)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    url,
                    params={
                        "category": category,
                        "symbol": symbol,
                        "interval": interval,
                        "start": start,
                        "end": end,
                        "limit": 2,
                    },
                )
                r.raise_for_status()
                payload = r.json()
                if str(payload.get("retCode")) != "0":
                    return None
                lst = (((payload.get("result") or {}).get("list")) or [])
                if not lst:
                    return None
                # Each item: [startTime, open, high, low, close, volume, turnover]
                rows = sorted(lst, key=lambda x: int(x[0]))
                # Find candle matching prev_start_ms
                for it in rows:
                    if int(it[0]) != start:
                        continue
                    o = _as_decimal(it[1])
                    h = _as_decimal(it[2])
                    l = _as_decimal(it[3])
                    cl = _as_decimal(it[4])
                    if _is_pos_finite_decimal(o) and _is_pos_finite_decimal(h) and _is_pos_finite_decimal(l) and _is_pos_finite_decimal(cl):
                        return o, h, l, cl
                # Fallback to first row if exact match not found.
                it = rows[0]
                o = _as_decimal(it[1])
                h = _as_decimal(it[2])
                l = _as_decimal(it[3])
                cl = _as_decimal(it[4])
                if _is_pos_finite_decimal(o) and _is_pos_finite_decimal(h) and _is_pos_finite_decimal(l) and _is_pos_finite_decimal(cl):
                    return o, h, l, cl
        except Exception:
            return None
        return None

    async def _trailing_decision_async(self, *, trade_direction: str, current_sl: Optional[Decimal]) -> dict:
        """
        Returns a dict with detailed trailing decision info for debugging.
        """
        out: dict[str, object] = {"direction": trade_direction, "eligible": False, "reason": None}
        if current_sl is None or not _is_pos_finite_decimal(current_sl):
            out["reason"] = "current_sl_invalid"
            return out
        if not bool(getattr(self.cfg, "trailing_stop_enabled", False)):
            out["reason"] = "disabled"
            return out
        mode = str(getattr(self.cfg, "trailing_candle_filter", "all") or "all").strip().lower()
        if mode not in {"all", "opposite"}:
            mode = "all"
        out["mode"] = mode
        src = str(getattr(self.cfg, "trailing_prev_candle_source", "auto") or "auto").strip().lower()
        if src not in {"state", "bybit_klines", "auto"}:
            src = "auto"
        out["candle_source"] = src

        d = (trade_direction or "").strip().upper()
        if d not in {"LONG", "SHORT"}:
            out["reason"] = "direction_invalid"
            return out

        o: Optional[Decimal] = None
        h: Optional[Decimal] = None
        l: Optional[Decimal] = None
        c: Optional[Decimal] = None
        # 1) Try state candle first (unless forced bybit_klines)
        if src in {"state", "auto"}:
            try:
                o0 = _as_decimal(self.state.get("prev_candle_open_price"))
                c0 = _as_decimal(self.state.get("prev_candle_close_price"))
                if _is_pos_finite_decimal(o0) and _is_pos_finite_decimal(c0):
                    o, c = o0, c0
                    # High/low are optional for body-based trailing; fill if present, else infer from O/C.
                    try:
                        h0 = _as_decimal(self.state.get("prev_candle_high_price"))
                        if _is_pos_finite_decimal(h0):
                            h = h0
                    except Exception:
                        pass
                    try:
                        l0 = _as_decimal(self.state.get("prev_candle_low_price"))
                        if _is_pos_finite_decimal(l0):
                            l = l0
                    except Exception:
                        pass
                    if h is None:
                        h = max(o, c)
                    if l is None:
                        l = min(o, c)
                    out["candle_source_used"] = "state"
            except Exception:
                pass

        # 2) Fallback to Bybit klines if requested/needed
        if (o is None or c is None) and src in {"bybit_klines", "auto"}:
            # Prefer bybit fallback when vwap_source is bybit_klines (typical case).
            vsrc = (self.cfg.vwap_source or "").strip().lower()
            # If user wants candles from Ethereal market prices, don't silently switch to Bybit in auto mode.
            candle_price_src = str(getattr(self.cfg, "trailing_candle_price_source", "market") or "market").strip().lower()
            allow_auto_bybit = not (src == "auto" and candle_price_src == "market")
            if src == "bybit_klines" or (vsrc == "bybit_klines" and allow_auto_bybit):
                tf_ms = self._timeframe_ms(self.cfg.timeframe)
                cur_start_ms = int(self.state.get("last_candle_start_ms") or 0)
                prev_start_ms = int(self.state.get("prev_candle_start_ms") or 0) or (cur_start_ms - tf_ms)
                if prev_start_ms > 0 and tf_ms > 0:
                    ohlc = await self._bybit_prev_candle_ohlc(prev_start_ms, tf_ms)
                    if ohlc is not None:
                        o, h, l, c = ohlc
                    out["candle_source_used"] = "bybit_klines"

        if o is None or c is None:
            out["reason"] = "prev_candle_missing"
            return out

        out["prev_open"] = o
        out["prev_high"] = h
        out["prev_low"] = l
        out["prev_close"] = c
        if o == c:
            out["reason"] = "doji"
            return out

        # Determine candle sign
        out["candle_sign"] = "bull" if c > o else "bear"

        body = (c - o).copy_abs()
        out["body"] = body
        if body <= 0:
            out["reason"] = "zero_body"
            return out

        # "all" mode: always tighten by abs body.
        if mode == "all":
            cand = (current_sl + body) if d == "LONG" else (current_sl - body)
            out["candidate_raw"] = cand
            out["eligible"] = True
            out["reason"] = "all_candles"
            return out

        # "opposite" mode (original rule)
        # LONG: act only on bearish candle
        if d == "LONG":
            if c >= o:
                out["reason"] = "not_bearish_for_long"
                return out
            cand = current_sl + (o - c)
            out["candidate_raw"] = cand
            out["eligible"] = True
            return out

        # SHORT: act only on bullish candle
        if c <= o:
            out["reason"] = "not_bullish_for_short"
            return out
        cand = current_sl - (c - o)
        out["candidate_raw"] = cand
        out["eligible"] = True
        return out

    def _cached_exit_levels(self) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Return cached (tp, sl) from state.exit_levels if both are valid."""
        levels = self.state.get("exit_levels") or {}
        tp_raw = levels.get("tp")
        sl_raw = levels.get("sl")
        try:
            tp = _as_decimal(tp_raw) if tp_raw is not None else None
        except Exception:
            tp = None
        try:
            sl = _as_decimal(sl_raw) if sl_raw is not None else None
        except Exception:
            sl = None
        if tp is not None and not _is_pos_finite_decimal(tp):
            tp = None
        if sl is not None and not _is_pos_finite_decimal(sl):
            sl = None
        return tp, sl

    def _set_pause_after_sl(
        self,
        minutes: int,
        *,
        vwap: Decimal,
        entry_px: Decimal,
        tp_px: Decimal,
        sl_px: Decimal,
        source: str,
        trade_direction: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        minutes = max(0, int(minutes))
        if minutes <= 0:
            return
        now = _utc_now()
        now_ts = int(now.timestamp())
        until_ts = now_ts + minutes * 60
        # If we're already paused for SL and the new deadline is nearly identical, skip duplicate pause logs.
        if self.state.get("trading_paused") and str(self.state.get("pause_reason") or "") == "SL_TIMER":
            cur_until = int(self.state.get("pause_until_ts") or 0)
            if cur_until and (until_ts - cur_until) <= 30:
                return
        sname = (strategy or self._cfg_strategy() or "").strip().lower() or "vwap"
        td = self._normalize_direction_value(
            trade_direction or self.state.get("anchor_open_direction") or self.state.get("vwap_trade_direction") or self.direction
        )
        if td not in {"LONG", "SHORT"}:
            td = "LONG"

        self.state["trading_paused"] = True
        self.state["pause_reason"] = "SL_TIMER"
        self.state["pause_source"] = str(source or "")
        self.state["pause_strategy"] = sname
        self.state["pause_trade_direction"] = td
        self.state["paused_at"] = now.isoformat()
        self.state["pause_until_ts"] = int(until_ts)
        self.state["pause_duration_min"] = int(minutes)
        self.state["pause_last_log_ms"] = 0
        # Clear old pause model target to avoid confusion.
        self.state["pause_release_price"] = None
        self._save_state()

        logger.warning(
            "PAUSED after SL: ticker=%s strategy=%s dir=%s minutes=%s until=%s source=%s vwap=%s entry=%s tp=%s sl=%s",
            self.cfg.ticker,
            sname,
            td,
            minutes,
            _ms_to_dt(until_ts * 1000).isoformat(),
            str(source or ""),
            str(vwap),
            str(entry_px),
            str(tp_px),
            str(sl_px),
        )

    def _pause_expired(self, now_ts: int) -> bool:
        until_ts = int(self.state.get("pause_until_ts") or 0)
        if until_ts <= 0:
            # If we have a pause flag without a deadline (legacy/corrupted state), auto-clear it.
            return True
        return now_ts >= until_ts

    def _log_pause_status(self, now_ms: int) -> None:
        """
        Log pause details with throttling (about once per minute).
        """
        last_ms = int(self.state.get("pause_last_log_ms") or 0)
        if last_ms and (now_ms - last_ms) < 60_000:
            return
        self.state["pause_last_log_ms"] = int(now_ms)
        self._save_state()

        until_ts = int(self.state.get("pause_until_ts") or 0)
        now_ts = int(now_ms / 1000)
        remaining = max(0, until_ts - now_ts) if until_ts > 0 else 0
        src = str(self.state.get("pause_source") or "")
        strat = str(self.state.get("pause_strategy") or self._cfg_strategy() or "")
        td = str(self.state.get("pause_trade_direction") or self.direction or "")
        paused_at = str(self.state.get("paused_at") or "")
        logger.info(
            "PAUSED: ticker=%s strategy=%s dir=%s remaining=%ss until=%s source=%s paused_at=%s reason=%s",
            self.cfg.ticker,
            strat,
            td,
            int(remaining),
            (_ms_to_dt(until_ts * 1000).isoformat() if until_ts > 0 else "unknown"),
            src,
            paused_at,
            str(self.state.get("pause_reason") or ""),
        )

    async def step(self) -> None:
        assert self.subaccount_id is not None and self.product_id is not None

        now = _utc_now()
        now_ms = _dt_to_ms(now)

        # Anchor-based VWAP (like original).
        anchor_start = self._anchor_start(now)
        anchor_start_ms = _dt_to_ms(anchor_start)
        prev_anchor = int(self.state.get("last_anchor_start_ms") or 0)
        anchor_changed = prev_anchor != anchor_start_ms
        if anchor_changed:
            # Persist last VWAP of previous anchor (TP reference for anchor_open).
            prev_vwap = self._decimal_from_state("anchor_last_vwap") or self._decimal_from_state("last_vwap")
            if prev_vwap is not None:
                self.state["prev_anchor_vwap"] = str(prev_vwap)
                self.state["prev_anchor_start_ms"] = int(prev_anchor or 0)

            # Reset anchor_open per-anchor markers
            self.state["anchor_open_open_price"] = None
            self.state["anchor_open_open_price_ms"] = 0
            self.state["anchor_open_decision_anchor_ms"] = 0
            self.state["anchor_open_last_wait_log_ms"] = 0

            # Reset VWAP auto-direction markers
            self.state["vwap_anchor_open_price"] = None
            self.state["vwap_anchor_open_price_ms"] = 0
            self.state["vwap_anchor_decision_anchor_ms"] = 0
            self.state["vwap_anchor_direction"] = None
            self.state["vwap_anchor_delta_pct"] = None
            self.state["vwap_anchor_last_wait_log_ms"] = 0

            # Reset per-anchor VWAP tracker for the new anchor.
            self.state["anchor_last_vwap"] = None
            self.state["anchor_last_vwap_ms"] = 0
            self.state["anchor_last_vwap_anchor_ms"] = int(anchor_start_ms)

            # Reset VWAP accumulators for new anchor period
            self.state["last_anchor_start_ms"] = anchor_start_ms
            self.state["cum_pq"] = "0"
            self.state["cum_q"] = "0"
            self.state["last_trade_ts"] = anchor_start_ms
            self._save_state()
            await self._cancel_entry()
            # Important safety rule:
            # do NOT cancel TP/SL on anchor change — VWAP can be NaN for a while at new anchor start.
            # We'll refresh exits later when we have valid levels.
            logger.info("New anchor period: %s", anchor_start.isoformat())

        # Candle boundary (timeframe) for "full refresh" logic (like original).
        tf_ms = self._timeframe_ms(self.cfg.timeframe)
        candle_start_ms = now_ms - (now_ms % tf_ms)
        is_new_candle = int(self.state.get("last_candle_start_ms") or 0) != candle_start_ms
        if is_new_candle:
            # Finalize previous candle tracking (for trailing stop)
            self.state["prev_candle_open_price"] = self.state.get("candle_open_price")
            self.state["prev_candle_close_price"] = self.state.get("candle_close_price")
            self.state["prev_candle_high_price"] = self.state.get("candle_high_price")
            self.state["prev_candle_low_price"] = self.state.get("candle_low_price")
            self.state["prev_candle_start_ms"] = int(self.state.get("candle_start_ms") or 0)
            # Reset current candle
            self.state["candle_open_price"] = None
            self.state["candle_close_price"] = None
            self.state["candle_high_price"] = None
            self.state["candle_low_price"] = None
            self.state["candle_start_ms"] = int(candle_start_ms)

            self.state["last_candle_start_ms"] = candle_start_ms
            self._save_state()
            if self.state.get("trading_paused"):
                until_ts = int(self.state.get("pause_until_ts") or 0)
                now_ts = int(now.timestamp())
                remaining = max(0, until_ts - now_ts) if until_ts > 0 else 0
                logger.info(
                    "NEW CANDLE %s — PAUSED ticker=%s strategy=%s dir=%s remaining=%ss until=%s",
                    _ms_to_dt(candle_start_ms).isoformat(),
                    self.cfg.ticker,
                    str(self.state.get("pause_strategy") or self._cfg_strategy() or ""),
                    str(self.state.get("pause_trade_direction") or self.direction or ""),
                    int(remaining),
                    (_ms_to_dt(until_ts * 1000).isoformat() if until_ts > 0 else "unknown"),
                )
            else:
                logger.info("NEW CANDLE %s — full refresh", _ms_to_dt(candle_start_ms).isoformat())
            self._trailing_debug(
                "TRAIL DEBUG: new_candle_start=%s prev_candle_start=%s prev_open=%s prev_close=%s",
                _ms_to_dt(candle_start_ms).isoformat(),
                (_ms_to_dt(int(self.state.get("prev_candle_start_ms") or 0)).isoformat() if int(self.state.get("prev_candle_start_ms") or 0) else "0"),
                str(self.state.get("prev_candle_open_price")),
                str(self.state.get("prev_candle_close_price")),
            )

        # Network errors can happen; keep trading loop alive.
        try:
            price = await self.get_oracle_price()
        except Exception as e:
            if _is_connect_error(e):
                now_ms_local = _dt_to_ms(_utc_now())
                last_log = int(self.state.get("last_connect_error_log_ms") or 0)
                if not last_log or (now_ms_local - last_log) > 30_000:
                    self.state["last_connect_error_log_ms"] = now_ms_local
                    self._save_state()
                    logger.warning("Network error fetching price (%s %s): %s", self.direction, self.cfg.ticker, type(e).__name__)
                price = Decimal("NaN")
            else:
                raise
        try:
            vwap = await self._get_vwap(anchor_start_ms, now_ms)
        except Exception as e:
            if _is_connect_error(e):
                now_ms_local = _dt_to_ms(_utc_now())
                last_log = int(self.state.get("last_connect_error_log_ms") or 0)
                if not last_log or (now_ms_local - last_log) > 30_000:
                    self.state["last_connect_error_log_ms"] = now_ms_local
                    self._save_state()
                    logger.warning("Network error fetching vwap (%s %s): %s", self.direction, self.cfg.ticker, type(e).__name__)
                vwap = Decimal("NaN")
            else:
                raise
        # Reference price for post-only repricing and (optionally) candle tracking:
        # Prefer market price; if unavailable (0/NaN), fall back to VWAP (useful when vwap_source=bybit_klines).
        ref_price = price if _is_pos_finite_decimal(price) else (vwap if _is_pos_finite_decimal(vwap) else Decimal("NaN"))

        # For VWAP auto-direction: capture anchor open price as soon as we have a valid reference price.
        self._maybe_capture_vwap_anchor_open_price(anchor_start_ms=anchor_start_ms, now_ms=now_ms, price=ref_price)

        # Candle price source (for trailing candles):
        candle_price_src = str(getattr(self.cfg, "trailing_candle_price_source", "market") or "market").strip().lower()
        if candle_price_src not in {"market", "ref"}:
            candle_price_src = "market"
        candle_price = price if candle_price_src == "market" else ref_price

        # Persist last known reference price snapshot for robust anchor transitions and repricing.
        if _is_pos_finite_decimal(ref_price):
            prev_saved = self._price_from_state("last_price") or Decimal("0")
            self.state["last_price"] = str(ref_price)
            self.state["last_price_ms"] = int(now_ms)
            self.state["last_price_anchor_ms"] = int(anchor_start_ms)
            if prev_saved <= 0 or (ref_price - prev_saved).copy_abs() / ref_price > Decimal("0.0005"):
                self._save_state()

        # Update candle open/high/low/close tracking from selected candle price.
        if _is_pos_finite_decimal(candle_price):
            if int(self.state.get("candle_start_ms") or 0) != int(candle_start_ms):
                self.state["candle_start_ms"] = int(candle_start_ms)
                self.state["candle_open_price"] = None
                self.state["candle_close_price"] = None
                self.state["candle_high_price"] = None
                self.state["candle_low_price"] = None
            if self.state.get("candle_open_price") is None:
                self.state["candle_open_price"] = str(candle_price)
                self.state["candle_high_price"] = str(candle_price)
                self.state["candle_low_price"] = str(candle_price)
            self.state["candle_close_price"] = str(candle_price)
            # Update high/low
            try:
                hi = _as_decimal(self.state.get("candle_high_price"))
            except Exception:
                hi = candle_price
            try:
                lo = _as_decimal(self.state.get("candle_low_price"))
            except Exception:
                lo = candle_price
            if _is_pos_finite_decimal(hi) and candle_price > hi:
                self.state["candle_high_price"] = str(candle_price)
            if _is_pos_finite_decimal(lo) and candle_price < lo:
                self.state["candle_low_price"] = str(candle_price)
            self._trailing_debug(
                "TRAIL DEBUG: candle_update ticker=%s candle_start=%s o=%s h=%s l=%s c=%s ref=%s (market=%s vwap=%s)",
                self.cfg.ticker,
                _ms_to_dt(int(self.state.get("candle_start_ms") or 0)).isoformat(),
                str(self.state.get("candle_open_price")),
                str(self.state.get("candle_high_price")),
                str(self.state.get("candle_low_price")),
                str(self.state.get("candle_close_price")),
                str(candle_price),
                str(price),
                str(vwap),
            )

        # Persist last known VWAP snapshot for anchor_open TP (works for any vwap_source).
        if (vwap == vwap) and vwap > 0:
            prev_saved_vwap = self._decimal_from_state("last_vwap") or Decimal("0")
            self.state["last_vwap"] = str(vwap)
            self.state["last_vwap_ms"] = int(now_ms)
            self.state["last_vwap_anchor_ms"] = int(anchor_start_ms)
            if prev_saved_vwap <= 0 or (vwap - prev_saved_vwap).copy_abs() / vwap > Decimal("0.0005"):
                self._save_state()

            # Also persist "last VWAP of this anchor" (updated only when anchor matches).
            prev_anchor_vwap = self._decimal_from_state("anchor_last_vwap") or Decimal("0")
            self.state["anchor_last_vwap"] = str(vwap)
            self.state["anchor_last_vwap_ms"] = int(now_ms)
            self.state["anchor_last_vwap_anchor_ms"] = int(anchor_start_ms)
            if prev_anchor_vwap <= 0 or (vwap - prev_anchor_vwap).copy_abs() / vwap > Decimal("0.0005"):
                self._save_state()

        pos = await self._get_open_position()
        pos_size = _as_decimal((pos or {}).get("size") or "0").copy_abs() if pos else Decimal("0")
        has_pos = bool(pos and pos_size > 0)

        # Persist last position size to detect transitions reliably.
        prev_size = _as_decimal(self.state.get("last_position_size") or "0").copy_abs()
        self.state["last_position_size"] = str(pos_size)
        # Track consecutive "no position" observations to avoid acting on transient position API glitches.
        try:
            streak = int(self.state.get("no_position_streak") or 0)
        except Exception:
            streak = 0
        if has_pos:
            streak = 0
        else:
            streak = min(10_000, streak + 1)
        self.state["no_position_streak"] = int(streak)
        # NOTE: we save state only on meaningful events to avoid excessive writes.

        opened_transition = (not _is_pos_finite_decimal(prev_size) or prev_size <= 0) and pos_size > 0
        if has_pos and not bool(self.state.get("opened_since_startup")):
            self.state["opened_since_startup"] = True
            self.state["opened_since_startup_ms"] = int(now_ms)
            self._save_state()

        trade_dir = self._resolve_vwap_auto_direction(
            anchor_start_ms=anchor_start_ms,
            now_ms=now_ms,
            price=ref_price,
            has_pos=has_pos,
            pos=pos,
        )
        if opened_transition:
            od = trade_dir or self._infer_direction_from_position(pos) or self._pause_trade_direction()
            logger.info("POSITION OPENED: ticker=%s dir=%s size=%s", self.cfg.ticker, str(od), str(pos_size))
        entry_px, tp_px, sl_px = self._levels(vwap, direction=trade_dir)

        # Handle SL/TP fills even if position visibility lags.
        handled = await self._check_and_handle_exit_fills(
            vwap=vwap,
            entry_px=entry_px,
            tp_px=tp_px,
            sl_px=sl_px,
            trade_direction=trade_dir,
        )
        if handled:
            return

        # Common close detection (all strategies): if position isn't visible, detect SL/TP and apply pause.
        if not has_pos:
            handled_close = await self._handle_no_position_close_and_pause(
                prev_pos_size=prev_size,
                pos_size=pos_size,
                vwap=vwap,
                entry_px=entry_px,
                tp_px=tp_px,
                sl_px=sl_px,
                trade_direction=trade_dir,
            )
            if handled_close:
                return

        # Handle pause-after-SL (timer model)
        if self.state.get("trading_paused"):
            now_ts = int(now.timestamp())
            if self._pause_expired(now_ts):
                # Clear pause and resume.
                self.state["trading_paused"] = False
                self.state["pause_reason"] = None
                self.state["paused_at"] = None
                self.state["pause_until_ts"] = 0
                self.state["pause_duration_min"] = 0
                self.state["pause_last_log_ms"] = 0
                self.state["pause_release_price"] = None
                self._save_state()
                logger.warning(
                    "PAUSE cleared: %s %s resumed.",
                    str(self.state.get("pause_trade_direction") or self._pause_trade_direction()),
                    self.cfg.ticker,
                )
            else:
                self._log_pause_status(now_ms)
                return

        # Strategy switch
        if self._cfg_strategy() == "anchor_open":
            await self._anchor_open_step(
                now=now,
                now_ms=now_ms,
                anchor_start_ms=anchor_start_ms,
                prev_anchor_ms=prev_anchor,
                price=ref_price,
                vwap=vwap,
                pos=pos,
                has_pos=has_pos,
                is_new_candle=is_new_candle,
            )
            return

        if self._cfg_strategy() == "vwap" and trade_dir is None:
            if has_pos:
                return
            if self.state.get("exit_order_ids"):
                return
            if self.state.get("entry_order_id"):
                await self._cancel_entry()
                self._save_state()
            self._clear_vwap_trade_direction()
            return

        if not has_pos:
            # If we still have active exits recorded, do not place new entries (position visibility can lag).
            if self.state.get("exit_order_ids"):
                return

            # On every new candle, re-place entry at fresh level (like original).
            if is_new_candle:
                await self._cancel_entry()
                logger.info(
                    "NEW CANDLE refresh: vwap=%s entry=%s tp=%s sl=%s (source=%s)",
                    str(vwap),
                    str(entry_px),
                    str(tp_px),
                    str(sl_px),
                    str(self.cfg.vwap_source),
                )
            await self._ensure_entry_order(entry_px, ref_price=ref_price, direction=trade_dir)
            return

        # Position exists:
        # - Cancel stale entry if still recorded
        if self.state.get("entry_order_id"):
            await self._cancel_entry()
            self._save_state()

        # Ensure exits exist (OCO TP+SL).
        # On each new candle we refresh:
        # - TP always follows the newly computed level
        # - SL only tightens (reduces risk); widening is ignored.
        if is_new_candle:
            # If we don't have valid VWAP-derived levels, keep existing exits untouched.
            if not _is_pos_finite_decimal(tp_px) or not _is_pos_finite_decimal(sl_px):
                if self.state.get("exit_order_ids"):
                    logger.info("EXITS unchanged: levels unavailable (tp=%s sl=%s). Keeping existing TP/SL.", str(tp_px), str(sl_px))
                else:
                    # If exits are missing but we have cached levels, place from cache for safety.
                    c_tp, c_sl = self._cached_exit_levels()
                    if c_tp is not None and c_sl is not None:
                        logger.warning(
                            "EXITS missing and levels unavailable; restoring from cache TP=%s SL=%s",
                            str(c_tp),
                            str(c_sl),
                        )
                        await self._ensure_oco_exits(pos, c_tp, c_sl, direction=trade_dir)
                    else:
                        logger.warning("EXITS missing and levels unavailable (tp=%s sl=%s). Position may be unprotected.", str(tp_px), str(sl_px))
                return

            # If exit levels cache is missing but we have exit orders, hydrate it from API to avoid widening SL.
            if self.state.get("exit_order_ids") and (not (self.state.get("exit_levels") or {}).get("sl")):
                await self._hydrate_exit_levels_from_api()

            cur_sl: Optional[Decimal] = None
            try:
                cur_sl_raw = ((self.state.get("exit_levels") or {}).get("sl"))
                cur_sl = _as_decimal(cur_sl_raw) if cur_sl_raw is not None else None
            except Exception:
                cur_sl = None

            # If we still don't know the current SL but exits exist, do not refresh (could widen accidentally).
            if self.state.get("exit_order_ids") and not _is_pos_finite_decimal(cur_sl or Decimal("NaN")):
                logger.info("EXITS unchanged: current SL unknown (missing cache). Keeping existing TP/SL.")
                return

            # Start with VWAP-derived SL tightening.
            new_sl = self._tightened_sl(current_sl=cur_sl, desired_sl=sl_px, trade_direction=trade_dir)
            # Apply trailing stop rule (relative to current SL, using prev candle body).
            trail_info = await self._trailing_decision_async(trade_direction=trade_dir or "", current_sl=cur_sl)
            trail = None
            if bool(trail_info.get("eligible")):
                try:
                    trail_raw = trail_info.get("candidate_raw")
                    trail = _as_decimal(trail_raw) if trail_raw is not None else None
                except Exception:
                    trail = None
            self._trailing_debug(
                "TRAIL DEBUG: vwap trailing decision ticker=%s dir=%s eligible=%s reason=%s prev_o=%s prev_h=%s prev_l=%s prev_c=%s body=%s current_sl=%s candidate_raw=%s",
                self.cfg.ticker,
                trade_dir,
                str(trail_info.get("eligible")),
                str(trail_info.get("reason")),
                str(trail_info.get("prev_open")),
                str(trail_info.get("prev_high")),
                str(trail_info.get("prev_low")),
                str(trail_info.get("prev_close")),
                str(trail_info.get("body")),
                str(cur_sl),
                str(trail_info.get("candidate_raw")),
            )
            if trail is not None and _is_pos_finite_decimal(trail):
                # Round trailing candidate to tick size before comparing/applying.
                trail_r = self._round_price(trail)
                new_sl = self._tightened_sl(current_sl=new_sl, desired_sl=trail_r, trade_direction=trade_dir)

            # If we have exits, refresh them with the tightened SL logic.
            if self.state.get("exit_order_ids"):
                # If nothing changes after rounding, keep exits (avoid cancel/recreate gaps).
                try:
                    cur_tp_raw = ((self.state.get("exit_levels") or {}).get("tp"))
                    cur_tp = _as_decimal(cur_tp_raw) if cur_tp_raw is not None else None
                except Exception:
                    cur_tp = None
                if _is_pos_finite_decimal(cur_tp or Decimal("NaN")) and _is_pos_finite_decimal(cur_sl or Decimal("NaN")):
                    r_tp_new = self._round_price(tp_px)
                    r_sl_new = self._round_price(new_sl)
                    r_tp_cur = self._round_price(cur_tp)  # type: ignore[arg-type]
                    r_sl_cur = self._round_price(cur_sl)  # type: ignore[arg-type]
                    if r_tp_new == r_tp_cur and r_sl_new == r_sl_cur:
                        return

                # Log when we refuse to widen SL
                if cur_sl is not None and _is_pos_finite_decimal(cur_sl) and _is_pos_finite_decimal(sl_px):
                    if trade_dir == "LONG" and sl_px < cur_sl:
                        logger.info("SL not widened (LONG): current_sl=%s desired_sl=%s -> keeping %s", str(cur_sl), str(sl_px), str(new_sl))
                    if trade_dir == "SHORT" and sl_px > cur_sl:
                        logger.info("SL not widened (SHORT): current_sl=%s desired_sl=%s -> keeping %s", str(cur_sl), str(sl_px), str(new_sl))
                if cur_sl is not None and _is_pos_finite_decimal(cur_sl) and trail is not None and _is_pos_finite_decimal(trail):
                    logger.info(
                        "Trailing SL candidate: prev_open=%s prev_close=%s current_sl=%s trail=%s -> chosen=%s",
                        str(trail_info.get("prev_open")),
                        str(trail_info.get("prev_close")),
                        str(cur_sl),
                        str(self._round_price(trail)),
                        str(self._round_price(new_sl)),
                    )

                await self._cancel_exits()
                await self._ensure_oco_exits(pos, tp_px, new_sl, direction=trade_dir)
            else:
                await self._ensure_oco_exits(pos, tp_px, new_sl, direction=trade_dir)
        else:
            # Outside new-candle refresh, still ensure we have exits.
            if not self.state.get("exit_order_ids"):
                if _is_pos_finite_decimal(tp_px) and _is_pos_finite_decimal(sl_px):
                    await self._ensure_oco_exits(pos, tp_px, sl_px, direction=trade_dir)
                else:
                    c_tp, c_sl = self._cached_exit_levels()
                    if c_tp is not None and c_sl is not None:
                        logger.warning("EXITS missing; restoring from cache TP=%s SL=%s", str(c_tp), str(c_sl))
                        await self._ensure_oco_exits(pos, c_tp, c_sl, direction=trade_dir)
            else:
                await self._ensure_oco_exits(pos, tp_px, sl_px, direction=trade_dir)

    async def run(self) -> None:
        logger.info("Starting VWAP strategy: %s %s", self.direction, self.cfg.ticker)
        while True:
            try:
                await self.step()
            except KeyboardInterrupt:
                raise
            except Exception:
                # Some exceptions (e.g., AssertionError) have empty str(e); keep full traceback.
                logger.exception("Loop error in %s %s", self.direction, self.cfg.ticker)
            await asyncio.sleep(int(self.cfg.poll_interval_sec))


def _load_or_create_config(path: str) -> tuple[StrategyConfig, bool]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        trades_page_limit = int(raw.get("trades_page_limit", 200))
        # clamp to API max
        trades_page_limit = min(max(trades_page_limit, 1), 200)
        return (
            StrategyConfig(
                strategy=str(raw.get("strategy", "vwap")),
                trailing_stop_enabled=bool(raw.get("trailing_stop_enabled", False)),
                trailing_candle_filter=str(raw.get("trailing_candle_filter", "all")),
                trailing_candle_price_source=str(raw.get("trailing_candle_price_source", "market")),
                trailing_prev_candle_source=str(raw.get("trailing_prev_candle_source", "auto")),
                ticker=str(raw.get("ticker", "SOLUSD")),
                direction=str(raw.get("direction", "LONG")).upper(),
                vwap_both_min_delta_pct=_as_decimal(raw.get("vwap_both_min_delta_pct", "0.1")),
                vwap_both_max_delta_pct=_as_decimal(raw.get("vwap_both_max_delta_pct", "0")),
                vwap_both_start_direction=str(raw.get("vwap_both_start_direction", "NONE")),
                vwap_both_mode=str(raw.get("vwap_both_mode", "trend")),
                poll_interval_sec=int(raw.get("poll_interval_sec", 3)),
                timeframe=str(raw.get("timeframe", "1h")),
                anchor_period=str(raw.get("anchor_period", "Session")),
                entry_distance_long_pct=_as_decimal(raw.get("entry_distance_long_pct", "1.0")),
                entry_distance_short_pct=_as_decimal(raw.get("entry_distance_short_pct", "1.5")),
                entry_quantity=_as_decimal(raw.get("entry_quantity", "0.001")),
                post_only=bool(raw.get("post_only", True)),
                entry_expires_in_sec=int(raw.get("entry_expires_in_sec", 7 * 24 * 3600)),
                tp_pct=_as_decimal(raw.get("tp_pct", "1.5")),
                sl_pct=_as_decimal(raw.get("sl_pct", "4.0")),
                exits_as_stop_market=bool(raw.get("exits_as_stop_market", True)),
                exit_expires_in_sec=int(raw.get("exit_expires_in_sec", 7 * 24 * 3600)),
                pause_on_sl=bool(raw.get("pause_on_sl", False)),
                pause_after_sl_minutes=int(raw.get("pause_after_sl_minutes", 0) or 0),
                pause_skip_on_startup=bool(raw.get("pause_skip_on_startup", False)),
                pause_ignore_sl_until_first_open=bool(raw.get("pause_ignore_sl_until_first_open", False)),
                anchor_open_min_delta_pct=_as_decimal(raw.get("anchor_open_min_delta_pct", raw.get("anchor_open_min_vwap_delta_pct", "0"))),
                anchor_open_sl_pct_of_tp=_as_decimal(raw.get("anchor_open_sl_pct_of_tp", "100")),
                anchor_open_close_on_anchor_end=bool(raw.get("anchor_open_close_on_anchor_end", True)),
                max_trade_pages=int(raw.get("max_trade_pages", 6)),
                trades_page_limit=trades_page_limit,
                vwap_recalc_threshold=_as_decimal(raw.get("vwap_recalc_threshold", "0.0005")),
                trade_overlap_ms=int(raw.get("trade_overlap_ms", 2000)),
                trade_id_cache_size=int(raw.get("trade_id_cache_size", 5000)),
                overflow_max_trade_pages=int(raw.get("overflow_max_trade_pages", 50)),
                vwap_source=str(raw.get("vwap_source", "ethereal_trades")),
                bybit_base_url=str(raw.get("bybit_base_url", "https://api.bybit.com")),
                bybit_category=str(raw.get("bybit_category", "linear")),
                bybit_symbol=str(raw.get("bybit_symbol", "SOLUSDT")),
                bybit_kline_interval=str(raw.get("bybit_kline_interval", "1")),
            ),
            False,
        )

    cfg = StrategyConfig()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "strategy": cfg.strategy,
                "trailing_stop_enabled": cfg.trailing_stop_enabled,
                "trailing_candle_filter": cfg.trailing_candle_filter,
                "trailing_candle_price_source": cfg.trailing_candle_price_source,
                "trailing_prev_candle_source": cfg.trailing_prev_candle_source,
                "ticker": cfg.ticker,
                "direction": cfg.direction,
                "vwap_both_min_delta_pct": str(cfg.vwap_both_min_delta_pct),
                "vwap_both_max_delta_pct": str(getattr(cfg, "vwap_both_max_delta_pct", Decimal("0"))),
                "vwap_both_start_direction": str(getattr(cfg, "vwap_both_start_direction", "NONE")),
                "vwap_both_mode": str(getattr(cfg, "vwap_both_mode", "trend")),
                "poll_interval_sec": cfg.poll_interval_sec,
                "timeframe": cfg.timeframe,
                "anchor_period": cfg.anchor_period,
                "entry_distance_long_pct": str(cfg.entry_distance_long_pct),
                "entry_distance_short_pct": str(cfg.entry_distance_short_pct),
                "entry_quantity": str(cfg.entry_quantity),
                "post_only": cfg.post_only,
                "entry_expires_in_sec": cfg.entry_expires_in_sec,
                "tp_pct": str(cfg.tp_pct),
                "sl_pct": str(cfg.sl_pct),
                "exits_as_stop_market": cfg.exits_as_stop_market,
                "exit_expires_in_sec": cfg.exit_expires_in_sec,
                "pause_on_sl": cfg.pause_on_sl,
                "pause_after_sl_minutes": cfg.pause_after_sl_minutes,
                "pause_skip_on_startup": bool(getattr(cfg, "pause_skip_on_startup", False)),
                "pause_ignore_sl_until_first_open": bool(getattr(cfg, "pause_ignore_sl_until_first_open", False)),
                "anchor_open_min_delta_pct": str(cfg.anchor_open_min_delta_pct),
                "anchor_open_sl_pct_of_tp": str(cfg.anchor_open_sl_pct_of_tp),
                "anchor_open_close_on_anchor_end": cfg.anchor_open_close_on_anchor_end,
                "max_trade_pages": cfg.max_trade_pages,
                "trades_page_limit": cfg.trades_page_limit,
                "vwap_recalc_threshold": str(cfg.vwap_recalc_threshold),
                "trade_overlap_ms": cfg.trade_overlap_ms,
                "trade_id_cache_size": cfg.trade_id_cache_size,
                "overflow_max_trade_pages": cfg.overflow_max_trade_pages,
                "vwap_source": cfg.vwap_source,
                "bybit_base_url": cfg.bybit_base_url,
                "bybit_category": cfg.bybit_category,
                "bybit_symbol": cfg.bybit_symbol,
                "bybit_kline_interval": cfg.bybit_kline_interval,
            },
            f,
            indent=2,
        )
    logger.info("Config created: %s (edit it and re-run)", path)
    return cfg, True


def _truthy_env(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in {"1", "true", "y", "yes", "on"}


def _parse_csv_upper(s: str) -> list[str]:
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


async def _retry_on_connect_error(name: str, coro_factory, *, base_delay: float = 1.0, max_delay: float = 30.0) -> Any:
    """
    Retry network calls on transient connect errors.
    coro_factory: zero-arg callable that returns an awaitable.
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as e:
            if not _is_connect_error(e):
                raise
            attempt += 1
            delay = min(max_delay, base_delay * (2 ** min(attempt, 6)))
            logger.warning("Network error during %s (attempt=%s). Retrying in %.1fs.", name, attempt, delay)
            await asyncio.sleep(delay)


def _strategy_config_from_json(raw: dict) -> StrategyConfig:
    trades_page_limit = int(raw.get("trades_page_limit", 200))
    trades_page_limit = min(max(trades_page_limit, 1), 200)  # clamp to API max
    return StrategyConfig(
        strategy=str(raw.get("strategy", "vwap")),
        trailing_stop_enabled=bool(raw.get("trailing_stop_enabled", False)),
        trailing_candle_filter=str(raw.get("trailing_candle_filter", "all")),
        trailing_candle_price_source=str(raw.get("trailing_candle_price_source", "market")),
        trailing_prev_candle_source=str(raw.get("trailing_prev_candle_source", "auto")),
        ticker=str(raw.get("ticker", "SOLUSD")).strip().upper(),
        direction=str(raw.get("direction", "LONG")).strip().upper(),
        vwap_both_min_delta_pct=_as_decimal(raw.get("vwap_both_min_delta_pct", "0.1")),
        vwap_both_max_delta_pct=_as_decimal(raw.get("vwap_both_max_delta_pct", "0")),
        vwap_both_start_direction=str(raw.get("vwap_both_start_direction", "NONE")),
        vwap_both_mode=str(raw.get("vwap_both_mode", "trend")),
        poll_interval_sec=int(raw.get("poll_interval_sec", 3)),
        timeframe=str(raw.get("timeframe", "1h")),
        anchor_period=str(raw.get("anchor_period", "Session")),
        entry_distance_long_pct=_as_decimal(raw.get("entry_distance_long_pct", "1.0")),
        entry_distance_short_pct=_as_decimal(raw.get("entry_distance_short_pct", "1.5")),
        entry_quantity=_as_decimal(raw.get("entry_quantity", "0.001")),
        post_only=bool(raw.get("post_only", True)),
        entry_expires_in_sec=int(raw.get("entry_expires_in_sec", 7 * 24 * 3600)),
        tp_pct=_as_decimal(raw.get("tp_pct", "1.5")),
        sl_pct=_as_decimal(raw.get("sl_pct", "4.0")),
        exits_as_stop_market=bool(raw.get("exits_as_stop_market", True)),
        exit_expires_in_sec=int(raw.get("exit_expires_in_sec", 7 * 24 * 3600)),
        pause_on_sl=bool(raw.get("pause_on_sl", False)),
        pause_after_sl_minutes=int(raw.get("pause_after_sl_minutes", 0) or 0),
        pause_skip_on_startup=bool(raw.get("pause_skip_on_startup", False)),
        pause_ignore_sl_until_first_open=bool(raw.get("pause_ignore_sl_until_first_open", False)),
        anchor_open_min_delta_pct=_as_decimal(raw.get("anchor_open_min_delta_pct", raw.get("anchor_open_min_vwap_delta_pct", "0"))),
        anchor_open_sl_pct_of_tp=_as_decimal(raw.get("anchor_open_sl_pct_of_tp", "100")),
        anchor_open_close_on_anchor_end=bool(raw.get("anchor_open_close_on_anchor_end", True)),
        max_trade_pages=int(raw.get("max_trade_pages", 6)),
        trades_page_limit=trades_page_limit,
        vwap_recalc_threshold=_as_decimal(raw.get("vwap_recalc_threshold", "0.0005")),
        trade_overlap_ms=int(raw.get("trade_overlap_ms", 2000)),
        trade_id_cache_size=int(raw.get("trade_id_cache_size", 5000)),
        overflow_max_trade_pages=int(raw.get("overflow_max_trade_pages", 50)),
        vwap_source=str(raw.get("vwap_source", "ethereal_trades")),
        bybit_base_url=str(raw.get("bybit_base_url", "https://api.bybit.com")),
        bybit_category=str(raw.get("bybit_category", "linear")),
        bybit_symbol=str(raw.get("bybit_symbol", "SOLUSDT")),
        bybit_kline_interval=str(raw.get("bybit_kline_interval", "1")),
    )


def _strategy_config_to_json(cfg: StrategyConfig) -> dict:
    return {
        "strategy": str(getattr(cfg, "strategy", "vwap")),
        "trailing_stop_enabled": bool(getattr(cfg, "trailing_stop_enabled", False)),
        "trailing_candle_filter": str(getattr(cfg, "trailing_candle_filter", "all")),
        "trailing_candle_price_source": str(getattr(cfg, "trailing_candle_price_source", "market")),
        "trailing_prev_candle_source": str(getattr(cfg, "trailing_prev_candle_source", "auto")),
        "ticker": cfg.ticker,
        "direction": cfg.direction,
        "vwap_both_min_delta_pct": str(getattr(cfg, "vwap_both_min_delta_pct", Decimal("0.1"))),
        "vwap_both_max_delta_pct": str(getattr(cfg, "vwap_both_max_delta_pct", Decimal("0"))),
        "vwap_both_start_direction": str(getattr(cfg, "vwap_both_start_direction", "NONE")),
        "vwap_both_mode": str(getattr(cfg, "vwap_both_mode", "trend")),
        "poll_interval_sec": cfg.poll_interval_sec,
        "timeframe": cfg.timeframe,
        "anchor_period": cfg.anchor_period,
        "entry_distance_long_pct": str(cfg.entry_distance_long_pct),
        "entry_distance_short_pct": str(cfg.entry_distance_short_pct),
        "entry_quantity": str(cfg.entry_quantity),
        "post_only": cfg.post_only,
        "entry_expires_in_sec": cfg.entry_expires_in_sec,
        "tp_pct": str(cfg.tp_pct),
        "sl_pct": str(cfg.sl_pct),
        "exits_as_stop_market": cfg.exits_as_stop_market,
        "exit_expires_in_sec": cfg.exit_expires_in_sec,
        "pause_on_sl": cfg.pause_on_sl,
        "pause_after_sl_minutes": int(getattr(cfg, "pause_after_sl_minutes", 0) or 0),
        "pause_skip_on_startup": bool(getattr(cfg, "pause_skip_on_startup", False)),
        "pause_ignore_sl_until_first_open": bool(getattr(cfg, "pause_ignore_sl_until_first_open", False)),
        "anchor_open_min_delta_pct": str(getattr(cfg, "anchor_open_min_delta_pct", Decimal("0"))),
        "anchor_open_sl_pct_of_tp": str(getattr(cfg, "anchor_open_sl_pct_of_tp", Decimal("100"))),
        "anchor_open_close_on_anchor_end": bool(getattr(cfg, "anchor_open_close_on_anchor_end", True)),
        "max_trade_pages": cfg.max_trade_pages,
        "trades_page_limit": cfg.trades_page_limit,
        "vwap_recalc_threshold": str(cfg.vwap_recalc_threshold),
        "trade_overlap_ms": cfg.trade_overlap_ms,
        "trade_id_cache_size": cfg.trade_id_cache_size,
        "overflow_max_trade_pages": cfg.overflow_max_trade_pages,
        "vwap_source": cfg.vwap_source,
        "bybit_base_url": cfg.bybit_base_url,
        "bybit_category": cfg.bybit_category,
        "bybit_symbol": cfg.bybit_symbol,
        "bybit_kline_interval": cfg.bybit_kline_interval,
    }


def _load_or_create_multi_config(path: str) -> tuple[list[StrategyConfig], int, bool]:
    """
    Multi-instrument config format:

    {
      "subaccount_index": 0,
      "strategies": [
        { ... StrategyConfig fields ... },
        { ... }
      ]
    }
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        sub_idx = int(raw.get("subaccount_index", 0))
        items = raw.get("strategies") or []
        cfgs = [_strategy_config_from_json(x or {}) for x in items]
        # de-dupe accidental empty entries
        cfgs = [c for c in cfgs if (c.ticker or "").strip()]
        return cfgs, sub_idx, False

    # Create a template file.
    tickers = _parse_csv_upper(os.getenv("ETHEREAL_TICKERS", ""))
    if not tickers:
        tickers = [(os.getenv("ETHEREAL_TICKER") or "SOLUSD").strip().upper()]
    sub_idx = int(os.getenv("ETHEREAL_SUBACCOUNT_INDEX", "0"))
    template_cfgs: list[StrategyConfig] = []
    for t in tickers:
        c = StrategyConfig(ticker=t)
        template_cfgs.append(c)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "subaccount_index": sub_idx,
                "strategies": [_strategy_config_to_json(c) for c in template_cfgs],
            },
            f,
            indent=2,
        )
    logger.info("Multi-config created: %s (edit it and re-run)", path)
    return template_cfgs, sub_idx, True


async def amain() -> None:
    testnet = (os.getenv("ETHEREAL_TESTNET", "1").strip().lower() in {"1", "true", "y", "yes"})
    network = "testnet" if testnet else "mainnet"
    base_url = os.getenv("ETHEREAL_BASE_URL") or ("https://api.etherealtest.net" if testnet else "https://api.ethereal.trade")
    rpc_url = os.getenv("ETHEREAL_RPC_URL") or ("https://rpc.etherealtest.net" if testnet else "https://rpc.ethereal.trade")
    private_key = (os.getenv("ETHEREAL_PRIVATE_KEY") or "").strip()
    if not private_key:
        raise RuntimeError("Set ETHEREAL_PRIVATE_KEY (EVM private key) in env.")

    client = await _retry_on_connect_error(
        "AsyncRESTClient.create()",
        lambda: AsyncRESTClient.create(
            {
                "network": network,
                "base_url": base_url,
                "chain_config": {
                    "rpc_url": rpc_url,
                    "private_key": private_key,
                },
            }
        ),
    )
    try:
        use_multi = os.path.exists(MULTI_CONFIG_FILE) or _truthy_env("ETHEREAL_MULTI") or bool(os.getenv("ETHEREAL_TICKERS"))

        if use_multi:
            cfgs, sub_idx_from_file, created = _load_or_create_multi_config(MULTI_CONFIG_FILE)
            if created:
                # Avoid continuing with defaults on first run (prevents confusing errors).
                return
            if not cfgs:
                raise RuntimeError(f"{MULTI_CONFIG_FILE} has no strategies. Add at least 1 strategy and re-run.")

            # Safety: this bot is NOT hedge-mode. Running multiple strategies on the same ticker in the same
            # subaccount will cause exit/position management conflicts and can stall trading (stale exits, cancels, etc.).
            # If duplicates are identical, keep the first entry and warn (helps recover from accidental copy/paste).
            by_ticker: dict[str, StrategyConfig] = {}
            dup: dict[str, list[StrategyConfig]] = {}
            mismatched: dict[str, list[StrategyConfig]] = {}
            deduped_cfgs: list[StrategyConfig] = []
            for c in cfgs:
                t = (c.ticker or "").strip().upper()
                if not t:
                    continue
                existing = by_ticker.get(t)
                if existing is None:
                    by_ticker[t] = c
                    deduped_cfgs.append(c)
                    continue
                dup.setdefault(t, [existing]).append(c)
                if c != existing:
                    mismatched[t] = dup[t]
            if mismatched:
                details = "; ".join(
                    f"{t}=" + ",".join(f"{(x.strategy or 'vwap')}:{(x.direction or '').upper()}" for x in lst)
                    for t, lst in sorted(mismatched.items())
                )
                raise RuntimeError(
                    "Invalid multi config: multiple different strategies for the same ticker in one subaccount. "
                    "This bot supports only ONE strategy per ticker per subaccount. "
                    "Remove duplicates or use separate subaccounts. "
                    f"Duplicates: {details}"
                )
            if dup:
                details = "; ".join(
                    f"{t}=" + ",".join(f"{(x.strategy or 'vwap')}:{(x.direction or '').upper()}" for x in lst)
                    for t, lst in sorted(dup.items())
                )
                logger.warning(
                    "Duplicate ticker entries in multi config; keeping first entry per ticker. "
                    "Remove duplicates or use separate subaccounts. Duplicates: %s",
                    details,
                )
                cfgs = deduped_cfgs

            # Optional global override for multi-mode.
            # We keep it explicit to avoid unintentionally overwriting per-instrument config.
            if os.getenv("ETHEREAL_ENTRY_QTY") and _truthy_env("ETHEREAL_ENTRY_QTY_ALL"):
                q = _as_decimal(os.getenv("ETHEREAL_ENTRY_QTY"))
                cfgs = [StrategyConfig(**{**c.__dict__, "entry_quantity": q}) for c in cfgs]
            elif os.getenv("ETHEREAL_ENTRY_QTY") and not _truthy_env("ETHEREAL_ENTRY_QTY_ALL"):
                logger.warning(
                    "ETHEREAL_ENTRY_QTY is set but ignored in multi-mode. "
                    "Set ETHEREAL_ENTRY_QTY_ALL=1 to apply it to all instruments."
                )

            sub_idx = int(os.getenv("ETHEREAL_SUBACCOUNT_INDEX", str(sub_idx_from_file)))

            # Shared discovery to reduce API calls for multi-run.
            subs = await _retry_on_connect_error("subaccounts()", lambda: client.subaccounts())
            if not subs:
                raise RuntimeError(
                    "No subaccounts found for this key. On Ethereal, a subaccount is created only after you "
                    "deposit USDe. Deposit (testnet: https://deposit.etherealtest.net, mainnet: https://deposit.ethereal.trade) "
                    "then re-run the bot."
                )
            if sub_idx < 0 or sub_idx >= len(subs):
                raise RuntimeError(f"subaccount_index={sub_idx} is out of range. Found {len(subs)} subaccounts.")
            sub_id = subs[sub_idx].id
            sub_name = subs[sub_idx].name

            products = await _retry_on_connect_error("products_by_ticker()", lambda: client.products_by_ticker())

            strategies: list[EtherealVWAPStrategy] = []
            for i, cfg in enumerate(cfgs):
                s = EtherealVWAPStrategy(client, cfg, subaccount_index=sub_idx)
                try:
                    await _retry_on_connect_error(
                        f"initialize({cfg.ticker})",
                        lambda: s.initialize(subaccount_id=sub_id, subaccount_name=sub_name, products_by_ticker=products),
                    )
                except RuntimeError as e:
                    logger.error("[%s %s] %s", cfg.direction, cfg.ticker, str(e))
                    continue
                strategies.append(s)
                # Small stagger to reduce bursts when many symbols start together.
                await asyncio.sleep(0.15 * i)

            if not strategies:
                raise RuntimeError("All strategies failed to initialize. Check tickers and config.")

            logger.info("Starting %d strategies in parallel.", len(strategies))
            await asyncio.gather(*(s.run() for s in strategies))
            return

        # Single-instrument mode (backward compatible)
        ticker = (os.getenv("ETHEREAL_TICKER") or "SOLUSD").strip().upper()
        direction = (os.getenv("ETHEREAL_DIRECTION") or "LONG").strip().upper()
        config_path = f"strategy_config_{direction}_{ticker}.json"
        cfg, created = _load_or_create_config(config_path)
        if created:
            return

        # allow env overrides (optional)
        if os.getenv("ETHEREAL_ENTRY_QTY"):
            cfg = StrategyConfig(**{**cfg.__dict__, "entry_quantity": _as_decimal(os.getenv("ETHEREAL_ENTRY_QTY"))})

        sub_idx = int(os.getenv("ETHEREAL_SUBACCOUNT_INDEX", "0"))
        strat = EtherealVWAPStrategy(client, cfg, subaccount_index=sub_idx)
        try:
            await _retry_on_connect_error("initialize(single)", lambda: strat.initialize())
        except RuntimeError as e:
            logger.error(str(e))
            return
        await strat.run()
    finally:
        await client.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()

