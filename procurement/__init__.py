"""Procurement rules assistant (prototype): NPC Procurement Guidelines/Manual -> clause index -> cited answers.

Separate from `billrag` on purpose: its own index (data/procurement/index.sqlite), its own citation labels
([PG2024 §2.7.1]); it reuses only the model adapters in `billrag.llm`.
"""
