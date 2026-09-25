"""Turns config/questions.yaml into TypeSafe SDK question objects."""

from typesafe_sdk import Choice, Noul, Question, Score

from app.config import QuestionConfig


def build_questions(cfg: QuestionConfig) -> dict[str, Question]:
    questions: dict[str, Question] = {}
    for name, instructions in cfg.nouls.items():
        criteria = cfg.noul_criteria.get(name)
        questions[name] = (
            Noul(instructions=instructions, criteria=criteria)
            if criteria
            else Noul(instructions=instructions)
        )
    for name, q in cfg.scores.items():
        questions[name] = Score(instructions=q.instructions, criteria=list(q.criteria))
    for name, (instructions, criteria) in cfg.choices.items():
        questions[name] = Choice(instructions=instructions, criteria=criteria)
    return questions
