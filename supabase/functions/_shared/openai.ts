// Must match EMBED_MODEL / DIMENSIONS in scripts/supabase_admin.py
// (the stored vectors are halfvec(1536) from the same model).
const EMBED_MODEL = "text-embedding-3-small";
const DIMENSIONS = 1536;

export function openaiKey(): string {
    const key = Deno.env.get("OPENAI_API_KEY");
    if (!key) throw new Error("OPENAI_API_KEY not set");
    return key;
}

// The question's embedding, in the same space as the stored sentences.
export async function embedQuestion(question: string): Promise<number[]> {
    const response = await fetch("https://api.openai.com/v1/embeddings", {
        method: "POST",
        headers: { "Authorization": `Bearer ${openaiKey()}`, "Content-Type": "application/json" },
        body: JSON.stringify({ model: EMBED_MODEL, input: question, dimensions: DIMENSIONS }),
    });
    if (!response.ok) throw new Error(`OpenAI embeddings ${response.status}: ${await response.text()}`);
    const data = await response.json();
    return data.data[0].embedding;
}
