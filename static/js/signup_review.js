/* Reuse the server's authentication proof-of-work protocol for public appeals. */
(function () {
    'use strict';
    const form = document.getElementById('signupReviewForm');
    if (!form) return;
    let ready = false;
    form.addEventListener('submit', async function (event) {
        if (ready) return;
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        try {
            const response = await fetch('/auth/challenge', {cache: 'no-store'});
            if (!response.ok) throw new Error('challenge');
            const challenge = await response.json();
            if (challenge.enabled !== false) {
                const prefix = '0'.repeat(challenge.difficulty || 3);
                let solved = false;
                const started = Date.now();
                for (let counter = 0; counter < (challenge.max_iterations || 1500000); counter++) {
                    if (Date.now() - started > ((challenge.ttl_seconds || 180) - 10) * 1000) break;
                    const bytes = new TextEncoder().encode(challenge.nonce + ':' + counter);
                    const hash = await crypto.subtle.digest('SHA-256', bytes);
                    const digest = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
                    if (digest.startsWith(prefix)) {
                        form.elements.challenge_id.value = challenge.challenge_id;
                        form.elements.challenge_counter.value = String(counter);
                        form.elements.challenge_digest.value = digest;
                        solved = true;
                        break;
                    }
                }
                if (!solved) throw new Error('expired');
            }
            ready = true;
            button.disabled = false;
            form.requestSubmit();
        } catch (error) {
            button.disabled = false;
            let alert = document.getElementById('reviewChallengeError');
            if (!alert) {
                alert = document.createElement('p');
                alert.id = 'reviewChallengeError';
                alert.setAttribute('role', 'alert');
                form.appendChild(alert);
            }
            alert.textContent = 'Security check failed. Refresh and try again.';
        }
    });
}());
