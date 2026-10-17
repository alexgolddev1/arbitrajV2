import asyncio
import random
import time
from typing import Dict

class TransferTimeEmulator:
    def __init__(self):
        # Эмпирическая статистика времени перевода (в секундах) с учетом:
        # [время подтверждения в блокчейне + время обработки биржей на ввод/вывод]
        self.network_profiles: Dict[str, dict] = {
            "TRON": {"avg": 180, "min": 60, "max": 350},      # TRC-20 (USDT)
            "BSC": {"avg": 120, "min": 45, "max": 240},       # BEP-20
            "SOLANA": {"avg": 30, "min": 10, "max": 90},       # SOL / SPL
            "TON": {"avg": 40, "min": 15, "max": 120},       # TON
            "ETHEREUM": {"avg": 600, "min": 240, "max": 1800}, # ERC-20 (зависит от газа)
            "BITCOIN": {"avg": 2400, "min": 900, "max": 5400}, # BTC
        }

    def get_emulated_duration(self, network: str) -> float:
        """
        Генерирует реалистичное время перевода с небольшим элементом 
        случайности (нормальное распределение / разброс из-за загрузки сети).
        """
        profile = self.network_profiles.get(network.upper(), {"avg": 300, "min": 120, "max": 600})
        
        # Генерируем случайное время в пределах профиля сети
        # Используем triangular-распределение, чтобы значения чаще были близки к среднему (avg)
        duration = random.triangular(profile["min"], profile["max"], profile["avg"])
        return round(duration, 2)

    async def emulate_transfer_process(self, transfer_id: str, network: str, initial_spread: float, price_checker_callback):
        """
        Эмулирует процесс перевода средств от старта до зачисления,
        параллельно отслеживая, живой ли еще спред на бирже-получателе.
        
        :param transfer_id: Уникальный ID тестовой симуляции
        :param network: Название сети (TRON, SOLANA и т.д.)
        :param initial_spread: Спред на момент фиксации арбитражной возможности (%)
        :param price_checker_callback: Функция (асинхронная), которая проверяет актуальный спред в реальном времени
        """
        duration = self.get_emulated_duration(network)
        print(f"[Emulator [{transfer_id}]] Старт перевода в сети {network}. Ожидаемое время: {duration:.1f} сек. Начальный спред: {initial_spread}%")
        
        start_time = time.time()
        elapsed = 0
        spread_history = []

        # Шаг проверки спреда во время «полета» средств (например, каждые 10 секунд)
        check_interval = 10 

        while elapsed < duration:
            await asyncio.sleep(min(check_interval, duration - elapsed))
            elapsed = time.time() - start_time
            
            # Запрашиваем текущий спред в стаканах бирж
            current_spread = await price_checker_callback()
            spread_history.append(current_spread)
            
            print(f"[{transfer_id}] Прошло {elapsed:.1f}с / {duration:.1f}с. Текущий спред: {current_spread}%")

            # Если в процессе перевода спред ушел в глубокий минус, фиксируем схлопывание
            if current_spread <= -0.5:
                print(f"[{transfer_id}] ❌ Спред схлопнулся во время перевода! Арбитраж ушел бы в убыток.")
                return {
                    "status": "failed_spread_died",
                    "duration_passed": elapsed,
                    "final_spread": current_spread,
                    "history": spread_history
                }

        print(f"[{transfer_id}] ✅ Перевод завершен успешно! Средства зачислены на биржу-получатель.")
        final_spread = spread_history[-1] if spread_history else initial_spread
        
        return {
            "status": "success",
            "duration": duration,
            "final_spread": final_spread,
            "history": spread_history
        }