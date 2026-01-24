"""Tests for intent classification system."""
import asyncio
from handlers.intent_router import RulesEngine, classify_intent
from utils.intent_helpers import IntentType, is_simple_action, needs_confirmation


def test_rules_engine():
    """Test fast pattern-based classification."""
    print("=== Testing Rules Engine ===\n")
    
    test_cases = [
        # Tasks
        ("надо купить молоко", IntentType.TASK),
        ("нужно доработать функцию", IntentType.TASK),
        ("сделать отчёт до завтра", IntentType.TASK),
        ("можешь добавить кнопку?", IntentType.TASK),
        
        # Reminders
        ("напомни мне через час", IntentType.REMINDER),
        ("через 30 минут позвонить Васе", IntentType.REMINDER),
        ("завтра напомни про встречу", IntentType.REMINDER),
        
        # Questions
        ("как работает эта функция?", IntentType.QUESTION),
        ("что такое рекурсия?", IntentType.QUESTION),
        ("почему не работает код?", IntentType.QUESTION),
        ("можешь помочь с багом?", IntentType.QUESTION),
        
        # Should not match
        ("привет, как дела?", None),
        ("согласен с тобой", None),
        ("отлично!", None),
    ]
    
    for text, expected_type in test_cases:
        result = RulesEngine.classify(text)
        detected_type = result.intent_type if result else None
        status = "✅" if detected_type == expected_type else "❌"
        print(f"{status} '{text}' -> {detected_type}")
    
    print()


def test_confidence_logic():
    """Test confidence and confirmation logic."""
    print("=== Testing Confidence Logic ===\n")
    
    from utils.intent_helpers import IntentResult
    
    # Simple task (no assignee, no deadline)
    simple_task = IntentResult(
        intent_type=IntentType.TASK,
        confidence=0.90,
        extracted_data={"task_text": "купить молоко"}
    )
    print(f"Simple task - is_simple: {is_simple_action(simple_task)}, needs_confirmation: {needs_confirmation(simple_task)}")
    
    # Complex task (with assignee)
    complex_task = IntentResult(
        intent_type=IntentType.TASK,
        confidence=0.90,
        extracted_data={"task_text": "купить молоко", "assignee": "@ivan"}
    )
    print(f"Complex task - is_simple: {is_simple_action(complex_task)}, needs_confirmation: {needs_confirmation(complex_task)}")
    
    # Low confidence task
    low_conf_task = IntentResult(
        intent_type=IntentType.TASK,
        confidence=0.70,
        extracted_data={"task_text": "что-то сделать"}
    )
    print(f"Low confidence task - is_simple: {is_simple_action(low_conf_task)}, needs_confirmation: {needs_confirmation(low_conf_task)}")
    
    # Question (always simple)
    question = IntentResult(
        intent_type=IntentType.QUESTION,
        confidence=0.80,
        extracted_data={"question": "как работает?"}
    )
    print(f"Question - is_simple: {is_simple_action(question)}, needs_confirmation: {needs_confirmation(question)}")
    
    print()


async def test_llm_classification():
    """Test LLM-based classification (requires API keys)."""
    print("=== Testing LLM Classification ===\n")
    
    test_cases = [
        "Вася, ты можешь подготовить презентацию к пятнице?",
        "Кто-нибудь может проверить этот код?",
        "Не забудь завтра отправить отчёт",
        "Как настроить автодеплой?",
    ]
    
    for text in test_cases:
        try:
            result = await classify_intent(text, context="группа")
            if result:
                print(f"✅ '{text}'")
                print(f"   Intent: {result.intent_type.value} (confidence: {result.confidence:.2f})")
                print(f"   Data: {result.extracted_data}")
            else:
                print(f"❌ '{text}' - No intent detected")
        except Exception as e:
            print(f"❌ '{text}' - Error: {e}")
        print()


def run_tests():
    """Run all tests."""
    print("🧪 Intent Classification System Tests\n")
    print("=" * 50)
    print()
    
    test_rules_engine()
    test_confidence_logic()
    
    # LLM tests require API keys
    print("=== LLM Classification Tests ===")
    print("⚠️  Requires YANDEX_GPT_API_KEY or OPENAI_API_KEY")
    print("Run manually: python tests/test_intent_classification.py --llm")
    print()


if __name__ == "__main__":
    import sys
    
    run_tests()
    
    if "--llm" in sys.argv:
        print("\nRunning LLM tests...")
        asyncio.run(test_llm_classification())

