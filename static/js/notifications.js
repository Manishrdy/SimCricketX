/**
 * SimCricketX Toast Notifications
 * A lightweight, dependency-free toast system.
 */

const Toast = {
    init() {
        // Create container if it doesn't exist
        if (!document.getElementById('toast-container')) {
            const container = document.createElement('div');
            container.id = 'toast-container';
            container.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 9999;
                display: flex;
                flex-direction: column;
                gap: 10px;
            `;
            document.body.appendChild(container);
        }
    },

    /**
     * Show a toast notification
     * @param {string} message - The text to display
     * @param {string} type - 'success', 'error', 'info', 'warning'
     * @param {number} duration - Time in ms before auto-dismiss (default 3000)
     */
    show(message, type = 'info', duration = 3000) {
        this.init();
        
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        
        // Icon selection
        let icon = 'fa-info-circle';
        if (type === 'success') icon = 'fa-check-circle';
        if (type === 'error' || type === 'danger') icon = 'fa-exclamation-circle';
        if (type === 'warning') icon = 'fa-exclamation-triangle';

        toast.innerHTML = `
            <i class="fas ${icon}"></i>
            <span>${message}</span>
            <button class="toast-close">&times;</button>
        `;

        // Styling (can be moved to CSS eventually, but inline ensures it works immediately)
        const colors = {
            success: '#10b981',
            error: '#ef4444',
            danger: '#ef4444',
            warning: '#f59e0b',
            info: '#3b82f6'
        };
        
        const bgColor = colors[type] || colors.info;

        toast.style.cssText = `
            background: white;
            color: #333;
            padding: 12px 16px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 300px;
            border-left: 4px solid ${bgColor};
            transform: translateX(100%);
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.3s ease;
            opacity: 0;
            font-family: 'Inter', sans-serif;
            font-size: 0.95rem;
        `;

        // Add icon color
        toast.querySelector('i').style.color = bgColor;
        
        // Close button style
        const closeBtn = toast.querySelector('.toast-close');
        closeBtn.style.cssText = `
            background: none;
            border: none;
            cursor: pointer;
            color: #999;
            font-size: 1.2rem;
            margin-left: auto;
            padding: 0 4px;
        `;
        
        closeBtn.onclick = () => this.dismiss(toast);

        // Add to DOM
        document.getElementById('toast-container').appendChild(toast);

        // Trigger animation
        requestAnimationFrame(() => {
            toast.style.transform = 'translateX(0)';
            toast.style.opacity = '1';
        });

        // Auto dismiss
        if (duration > 0) {
            setTimeout(() => {
                if (document.body.contains(toast)) {
                    this.dismiss(toast);
                }
            }, duration);
        }
    },

    dismiss(toast) {
        toast.style.transform = 'translateX(100%)';
        toast.style.opacity = '0';
        setTimeout(() => {
            if (toast.parentElement) {
                toast.parentElement.removeChild(toast);
            }
        }, 300);
    }
};

// Expose globally
window.Toast = Toast;
