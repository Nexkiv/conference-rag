// ============================================
// STUDENT CONFIGURATION FILE
// ============================================
// This file reads your Supabase credentials from config.public.json.
// config.public.json contains values that are SAFE to commit:
//   - Supabase URL
//   - Supabase publishable key (protected by Row Level Security)
//
// SECRET values (OpenAI key, Supabase secret key, access token) live only in
// environment variables on the machine that runs the scripts. Never in files.

// Default placeholder config — overwritten when config.public.json loads
const SUPABASE_CONFIG = {
    url: 'YOUR_SUPABASE_URL_HERE',
    publishableKey: 'YOUR_SUPABASE_PUBLISHABLE_KEY_HERE'
};

// Load config.public.json and update SUPABASE_CONFIG
fetch('config.public.json')
    .then(response => {
        if (!response.ok) throw new Error('config.public.json not found');
        return response.json();
    })
    .then(config => {
        if (config.SUPABASE_URL) SUPABASE_CONFIG.url = config.SUPABASE_URL;
        if (config.SUPABASE_PUBLISHABLE_KEY) SUPABASE_CONFIG.publishableKey = config.SUPABASE_PUBLISHABLE_KEY;
        // Re-initialize now that config is loaded
        window.dispatchEvent(new Event('config-loaded'));
    })
    .catch(err => {
        console.warn('⚠️ Could not load config.public.json:', err.message);
        console.warn('   Create config.public.json with your Supabase URL and publishable key.');
    });
