"""
CLI-сервис поиска поставщиков продуктов питания.

Поток данных:
    запрос на естественном языке -> LLM (instructor) -> SearchFilters (Pydantic)
    -> параметризованный SQL (sqlite3) -> list[Supplier] -> карточки (rich)

Переменные окружения:
    OPENAI_API_KEY   - ключ API 
    OPENAI_BASE_URL  - базовый URL 
    LLM_MODEL        - модель 
    SUPPLIERS_DB     - путь к SQLite-базе (по умолчанию "suppliers.db")
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Literal, Optional

import instructor
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dotenv import load_dotenv; load_dotenv()


# Конфигурация

DB_PATH = Path(os.getenv("SUPPLIERS_DB", "suppliers.db"))
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
RESULTS_LIMIT = 20
EXIT_COMMANDS = {"exit", "quit", "q", "выход"}

Category = Literal[
    "мясная продукция",
    "упаковка",
    "молочные продукты",
    "бакалея",
    "напитки",
    "кондитерские изделия",
]

console = Console()


class LLMError(RuntimeError):
    """Ошибка при обращении к LLM (сеть, авторизация, невалидный ответ)."""


# Pydantic-модели


class SearchFilters(BaseModel):
    """
    Фильтры поиска, извлекаемые LLM из запроса пользователя.
    Любое поле равно None, если пользователь о нём не упоминал.
    """

    model_config = ConfigDict(extra="forbid")

    category: Optional[Category] = Field(
        default=None,
        description=(
            "Категория продукции строго из разрешённого списка. "
            "Например, томатная паста, крупы, консервы, мука -> 'бакалея'."
        ),
    )
    city: Optional[str] = Field(
        default=None,
        description="Город поставщика в именительном падеже, например 'Москва'.",
    )
    region: Optional[str] = Field(
        default=None,
        description="Регион/область поставщика в именительном падеже, если указан.",
    )
    max_min_order: Optional[float] = Field(
        default=None,
        gt=0,
        description="Максимально допустимая минимальная партия (число, в единицах базы, кг).",
    )
    has_delivery: Optional[bool] = Field(
        default=None,
        description="True, только если пользователь явно требует доставку.",
    )
    has_certificates: Optional[bool] = Field(
        default=None,
        description="True, только если пользователь явно требует сертификаты.",
    )

    def is_empty(self) -> bool:
        """Возвращает True, если ни один фильтр не задан."""
        return not self.model_dump(exclude_none=True)


class Supplier(BaseModel):
    """Поставщик продуктов питания (строка таблицы `suppliers`)."""

    id: int
    name: str
    category: str
    city: str
    region: Optional[str] = None
    min_order: float
    price_info: Optional[str] = None
    has_delivery: bool
    has_certificates: bool
    contacts: Optional[str] = None
    website: Optional[str] = None


# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #


def init_db() -> None:
    """
    Проверяет наличие базы и таблицы `suppliers`.
    Raises:
        FileNotFoundError: если файл базы данных не найден.
        RuntimeError: если файл существует, но таблицы `suppliers` в нём нет.
    """
    if not DB_PATH.is_file():
        raise FileNotFoundError(
            f"База данных не найдена: '{DB_PATH}'. "
            "Создайте её или укажите путь в переменной SUPPLIERS_DB."
        )
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'suppliers'"
        ).fetchone()
    if row is None:
        raise RuntimeError(f"В базе '{DB_PATH}' отсутствует таблица 'suppliers'.")


# --------------------------------------------------------------------------- #
# LLM: извлечение фильтров
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "Ты извлекаешь параметры поиска поставщиков продуктов питания из запроса "
    "пользователя. Заполняй только те поля, которые явно следуют из запроса, "
    "остальные оставляй null. Не выдумывай значения. Город и регион указывай "
    "в именительном падеже. Фильтры has_delivery и has_certificates ставь "
    "в true только при явном требовании."
)


def _build_client() -> instructor.Instructor:
    """Создаёт instructor-клиент на основе OpenAI-совместимого API."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("Не задана переменная окружения OPENAI_API_KEY.")
    return instructor.from_openai(
        OpenAI(base_url=os.getenv("OPENAI_BASE_URL"), api_key=api_key)
    )


def extract_filters(user_query: str) -> SearchFilters:
    """
    Извлекает структурированные фильтры из запроса на естественном языке.

    Args:
        user_query: Текст запроса пользователя.

    Returns:
        Валидированная модель `SearchFilters`.

    Raises:
        LLMError: при любой ошибке взаимодействия с LLM.
    """
    try:
        client = _build_client()
        return client.chat.completions.create(
            model=LLM_MODEL,
            response_model=SearchFilters,
            max_retries=2,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
        )
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc


# Поиск


def search_suppliers(filters: SearchFilters) -> list[Supplier]:
    """
    Ищет поставщиков по фильтрам через параметризованный SQL.

    Args:
        filters: Фильтры поиска; пустые фильтры означают поиск по всем.

    Returns:
        Список найденных поставщиков (сначала с наименьшей минимальной партией).
    """
    conditions: list[str] = []
    params: list[object] = []

    # SQLite сравнивает регистр только для ASCII, поэтому для кириллицы
    # регистрируем casefold-функцию на стороне Python.
    if filters.category:
        conditions.append("category = ?")
        params.append(filters.category)
    if filters.city:
        conditions.append("casefold(city) = ?")
        params.append(filters.city.casefold())
    if filters.region:
        conditions.append("casefold(region) LIKE ?")
        params.append(f"%{filters.region.casefold()}%")
    if filters.max_min_order is not None:
        conditions.append("min_order <= ?")
        params.append(filters.max_min_order)
    if filters.has_delivery:
        conditions.append("has_delivery = 1")
    if filters.has_certificates:
        conditions.append("has_certificates = 1")

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        "SELECT id, name, category, city, region, min_order, price_info, "
        "has_delivery, has_certificates, contacts, website "
        f"FROM suppliers{where} ORDER BY min_order ASC LIMIT ?"
    )
    params.append(RESULTS_LIMIT)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.create_function(
            "casefold", 1, lambda v: v.casefold() if isinstance(v, str) else v,
            deterministic=True,
        )
        rows = conn.execute(query, params).fetchall()

    return [Supplier.model_validate(dict(row)) for row in rows]


# Вывод


def _flag(value: bool) -> str:
    """Форматирует булево значение для вывода."""
    return "[green]✔ да[/green]" if value else "[red]✘ нет[/red]"


def print_suppliers(suppliers: list[Supplier]) -> None:
    """
    Выводит карточки поставщиков в консоль через rich.
    Args:
        suppliers: Список поставщиков для отображения.
    """
    for supplier in suppliers:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", no_wrap=True)
        grid.add_column()
        location = ", ".join(filter(None, [supplier.city, supplier.region]))
        grid.add_row("Категория", supplier.category)
        grid.add_row("Локация", location)
        grid.add_row("Мин. партия", f"{supplier.min_order:g} кг")
        grid.add_row("Цены", supplier.price_info or "—")
        grid.add_row("Доставка", _flag(supplier.has_delivery))
        grid.add_row("Сертификаты", _flag(supplier.has_certificates))
        grid.add_row("Контакты", supplier.contacts or "—")
        grid.add_row("Сайт", supplier.website or "—")
        console.print(
            Panel(
                grid,
                title=f"[bold]{supplier.name}[/bold]",
                subtitle=f"#{supplier.id}",
                box=box.ROUNDED,
                border_style="blue",
                expand=False,
            )
        )


def _print_filters(filters: SearchFilters) -> None:
    """Выводит применённые фильтры одной строкой."""
    applied = filters.model_dump(exclude_none=True)
    text = ", ".join(f"{k}={v}" for k, v in applied.items())
    console.print(f"[dim]Фильтры: {text}[/dim]")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


def handle_query(user_query: str) -> None:
    """Обрабатывает один запрос: фильтры -> поиск -> вывод."""
    try:
        with console.status("[bold]Анализирую запрос...[/bold]"):
            filters = extract_filters(user_query)
    except LLMError as exc:
        console.print(f"[red]Не удалось обработать запрос через LLM:[/red] {exc}")
        return

    if filters.is_empty():
        console.print("[yellow]Фильтры не распознаны — ищу среди всех поставщиков.[/yellow]")
    else:
        _print_filters(filters)

    try:
        suppliers = search_suppliers(filters)
    except (sqlite3.Error, ValidationError) as exc:
        console.print(f"[red]Ошибка при работе с базой данных:[/red] {exc}")
        return

    if not suppliers:
        console.print(
            "[yellow]К сожалению, по вашему запросу ничего не найдено. "
            "Попробуйте смягчить условия.[/yellow]"
        )
        return

    console.print(f"[bold green]Найдено поставщиков: {len(suppliers)}[/bold green]")
    print_suppliers(suppliers)


def main() -> int:
    """Запускает интерактивный цикл CLI. Возвращает код выхода процесса."""
    try:
        init_db()
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        console.print(f"[red]Ошибка инициализации:[/red] {exc}")
        return 1

    console.print(
        Panel(
            "Опишите, какого поставщика вы ищете.\n"
            "Пример: [italic]Найди в Москве поставщиков томатной пасты с доставкой "
            "и сертификатами, минимальная партия до 500 кг[/italic]\n"
            "Для выхода: [bold]exit[/bold]",
            title="Поиск поставщиков",
            border_style="green",
        )
    )

    while True:
        try:
            user_query = console.input("\n[bold cyan]Запрос › [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nДо свидания!")
            return 0
        if not user_query:
            continue
        if user_query.lower() in EXIT_COMMANDS:
            console.print("До свидания!")
            return 0
        handle_query(user_query)


if __name__ == "__main__":
    sys.exit(main())