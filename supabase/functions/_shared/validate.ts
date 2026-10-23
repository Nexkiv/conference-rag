import { json } from "./cors.ts";

// Must match MAX_QUESTION_CHARS in scripts/supabase_admin.py (the my_questions
// CHECK constraint) and maxlength="..." on the inputs in index.html.
export const MAX_QUESTION_CHARS = 500;

// A 400 response for a missing, non-string or too-long question; null if it's fine.
export function invalidQuestion(question: unknown): Response | null {
    if (!question || typeof question !== "string") {
        return json({ error: "Missing or invalid 'question' field" }, 400);
    }
    if (question.length > MAX_QUESTION_CHARS) {
        return json({ error: `Question must be ${MAX_QUESTION_CHARS} characters or fewer` }, 400);
    }
    return null;
}
