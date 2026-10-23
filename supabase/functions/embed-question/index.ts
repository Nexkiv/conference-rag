import "jsr:@supabase/functions-js@2.118.0/edge-runtime.d.ts";
import { corsHeaders, json, MAX_QUESTION_CHARS, serverError } from "../_shared/cors.ts";
import { requireAuth } from "../_shared/auth.ts";

// Must match DIMENSIONS in scripts/04_embed_data.py and halfvec(1536) in the schema.
const DIMENSIONS = 1536;

Deno.serve(async (req) => {
    if (req.method === "OPTIONS") {
        return new Response("ok", { headers: corsHeaders });
    }

    const auth = await requireAuth(req);
    if ("error" in auth) return auth.error;

    try {
        const body = await req.json();

        // Health check from the page: proves deploy + login work, costs nothing.
        if (body.ping === true) return json({ ok: true });

        const { question } = body;
        if (!question || typeof question !== "string") {
            return json({ error: "Missing or invalid 'question' field" }, 400);
        }
        if (question.length > MAX_QUESTION_CHARS) {
            return json({ error: `Question must be ${MAX_QUESTION_CHARS} characters or fewer` }, 400);
        }

        const openaiKey = Deno.env.get("OPENAI_API_KEY");
        if (!openaiKey) return serverError("OPENAI_API_KEY not set", "Embedding service not configured");

        const response = await fetch("https://api.openai.com/v1/embeddings", {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${openaiKey}`,
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                model: "text-embedding-3-small",
                input: question,
                dimensions: DIMENSIONS,
            }),
        });

        if (!response.ok) {
            return serverError(await response.text(), "Embedding failed", 502);
        }

        const data = await response.json();
        return json({ embedding: data.data[0].embedding });
    } catch (err) {
        return serverError(err, "Embedding failed");
    }
});
