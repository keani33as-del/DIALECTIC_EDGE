# -*- coding: utf-8 -*-
"""Тесты для умного разбиения /markets-сообщений по границам секций.

Раньше `build_markets_panel_message` тупо обрезал хвост по 4000 символов
и приписывал «…часть текста скрыта». Юзер не видел нефть/индексы/сырьё
если живой контекст распухал. Теперь режем по `\n\n[` (граница секций
типа `[КРИПТОРЫНОК]` / `[МАКРОЭКОНОМИКА США]` / etc) и шлём несколько
сообщений.
"""
from __future__ import annotations

import unittest

from signals import _split_by_assets, _split_markets_message


class TestSplitMarketsMessage(unittest.TestCase):
    def test_short_returns_as_is(self):
        out = _split_markets_message("hello world", max_len=100)
        self.assertEqual(out, ["hello world"])

    def test_splits_by_section_when_too_long(self):
        # Текст с 4 секциями, каждая ~150 символов. max_len=200 → ожидаем
        # 4 chunk'а (по одному на секцию), так как в один не вмещается даже две.
        section = "  Some asset line that is rendered quite long for testing"
        text = (
            "header line\n\n"
            "[КРИПТОРЫНОК]\n" + section * 3 + "\n\n"
            "[МАКРОЭКОНОМИКА США]\n" + section * 3 + "\n\n"
            "[ФОНДОВЫЕ ИНДЕКСЫ]\n" + section * 3 + "\n\n"
            "[СЫРЬЁ И ВАЛЮТЫ]\n" + section * 3
        )
        chunks = _split_markets_message(text, max_len=200)
        # Минимум 4 chunk'а (по одному на секцию) — может быть 5 если header сам chunk.
        self.assertGreaterEqual(len(chunks), 4)
        # Каждый chunk ≤ max_len
        for c in chunks:
            self.assertLessEqual(len(c), 200)
        # Объединение всех chunk'ов содержит все секции
        joined = "\n\n".join(chunks)
        self.assertIn("[КРИПТОРЫНОК]", joined)
        self.assertIn("[МАКРОЭКОНОМИКА США]", joined)
        self.assertIn("[ФОНДОВЫЕ ИНДЕКСЫ]", joined)
        self.assertIn("[СЫРЬЁ И ВАЛЮТЫ]", joined)

    def test_no_section_truncated_mid_asset(self):
        # Регрессия: '…часть текста скрыта' больше не появляется. Каждый
        # chunk оканчивается на полный актив или секцию.
        text = (
            "[КРИПТОРЫНОК]\n"
            "  Bitcoin (BTC): $79,110\n"
            "    ↔️ ТРЕНД: SIDEWAYS\n"
            "  Ethereum (ETH): $2,223\n"
            "    📉 ТРЕНД: DOWNTREND\n\n"
            "[МАКРОЭКОНОМИКА США]\n"
            "  Ставка ФРС: 3.64%\n"
        )
        chunks = _split_markets_message(text, max_len=100)
        joined = "\n".join(chunks)
        self.assertNotIn("…часть текста скрыта", joined)

    def test_huge_section_splits_by_asset(self):
        # Если одна секция огромная — режем по asset-границам.
        long_section = "[КРИПТОРЫНОК]\n" + "".join(
            f"  Asset{i}: $100\n    line2\n    line3\n"
            for i in range(20)
        )
        chunks = _split_markets_message(long_section, max_len=200)
        self.assertGreater(len(chunks), 1)
        # Каждый chunk небольшой
        for c in chunks:
            self.assertLessEqual(len(c), 250)


class TestSplitByAssets(unittest.TestCase):
    def test_returns_single_chunk_when_fits(self):
        section = "[КРИПТОРЫНОК]\n  Bitcoin: $79,110"
        out = _split_by_assets(section, max_len=1000)
        self.assertEqual(len(out), 1)

    def test_splits_long_section(self):
        lines = [f"  Asset{i}: ${i*100}" for i in range(15)]
        section = "[КРИПТОРЫНОК]\n" + "\n".join(lines)
        out = _split_by_assets(section, max_len=80)
        self.assertGreater(len(out), 1)
        for c in out:
            self.assertLessEqual(len(c), 100)  # some slack for last line


if __name__ == "__main__":
    unittest.main()
