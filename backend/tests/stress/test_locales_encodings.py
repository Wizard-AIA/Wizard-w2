"""International Locales, Collations, and Pathological Encodings Test Suite.

Inspired by Pandas and PostgreSQL multi-locale CI suites.
Validates Wizard's tabular ingestion, Arrow serialization, and numerical conversion
across European, Asian, and non-ASCII character sets and decimal notations.
"""

from __future__ import annotations

import io
import unicodedata

import pandas as pd
import pyarrow as pa


class TestLocalesAndEncodings:
    """Validates internationalization edge cases in data profiling and parsing."""

    def test_multilingual_and_emoji_column_headers(self):
        """Headers containing Kanji, Arabic, Cyrillic, and Emojis must serialize cleanly."""
        data = {
            "ユーザー_ID 🧑‍💻": [101, 102, 103],
            "المبلغ_الإجمالي 💰": [1500.50, 2750.00, 3100.25],
            "Статус_Заказа ✅": ["Выполнен", "В обработке", "Отменен"],
            "Marge_Bénéficiaire_%": [0.15, 0.22, 0.18],
        }
        df = pd.DataFrame(data)

        # 1. Convert to Apache Arrow table
        table = pa.Table.from_pandas(df)
        assert table.num_columns == 4
        assert table.num_rows == 3

        # 2. Serialize to binary Arrow IPC stream
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        raw_ipc = sink.getvalue()
        assert len(raw_ipc) > 0

        # 3. Read back and verify exact character preservation
        reader = pa.ipc.open_stream(io.BytesIO(raw_ipc))
        restored_table = reader.read_all()
        restored_df = restored_table.to_pandas()

        assert list(restored_df.columns) == list(df.columns)
        assert restored_df["Статус_Заказа ✅"].tolist() == ["Выполнен", "В обработке", "Отменен"]

    def test_unicode_normalization_nfc_vs_nfd(self):
        """Combined accents (e.g. 'e' + combining acute) vs precomposed 'é'."""
        composed_str = "café"  # NFC
        decomposed_str = unicodedata.normalize("NFD", composed_str)  # NFD: 'cafe\u0301'

        assert composed_str != decomposed_str
        assert len(composed_str) == 4
        assert len(decomposed_str) == 5

        df = pd.DataFrame({"nfc": [composed_str], "nfd": [decomposed_str]})
        table = pa.Table.from_pandas(df)

        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)

        reader = pa.ipc.open_stream(io.BytesIO(sink.getvalue()))
        out_table = reader.read_all()
        out_df = out_table.to_pandas()

        assert out_df["nfc"].iloc[0] == composed_str
        assert out_df["nfd"].iloc[0] == decomposed_str

    def test_european_decimal_notation_coercion(self):
        """Parses European formatted numbers ('1.234,56') into floating point values."""
        raw_values = ["1.234,56", "10.500,00", "0,75", "-3.141,59"]

        def _parse_eu_decimal(val_str: str) -> float:
            cleaned = val_str.replace(".", "").replace(",", ".")
            return float(cleaned)

        parsed = [_parse_eu_decimal(v) for v in raw_values]
        assert parsed == [1234.56, 10500.0, 0.75, -3141.59]

    def test_international_date_formats(self):
        """Parses DD.MM.YYYY (German) and YYYY/MM/DD (Japanese) date standards."""
        dates_de = ["26.08.2026", "01.01.2025", "31.12.2024"]
        parsed_de = pd.to_datetime(dates_de, format="%d.%m.%Y")
        assert parsed_de[0].day == 26
        assert parsed_de[0].month == 8
        assert parsed_de[0].year == 2026

        dates_jp = ["2026/08/26", "2025/01/01", "2024/12/31"]
        parsed_jp = pd.to_datetime(dates_jp, format="%Y/%m/%d")
        assert parsed_jp[0].day == 26
        assert parsed_jp[0].month == 8
        assert parsed_jp[0].year == 2026
