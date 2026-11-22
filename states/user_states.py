"""Finite-state definitions for user interactions."""

from aiogram.fsm.state import State, StatesGroup


class PromoStates(StatesGroup):
    waiting_code = State()


class ShopStates(StatesGroup):
    waiting_receipt = State()


class SupportStates(StatesGroup):
    waiting_message = State()


class AdsStates(StatesGroup):
    waiting_date = State()
    waiting_brief = State()


class QuestStates(StatesGroup):
    waiting_proof = State()
