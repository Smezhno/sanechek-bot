// Telegram Web App API
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// API Base URL (will be set from bot)
const API_BASE = tg.initDataUnsafe?.start_param 
    ? `https://api.telegram.org/bot${tg.initDataUnsafe.start_param}/webapp`
    : '/api';

// State
let currentPage = 'tasks';
let currentFilter = 'all';

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initModals();
    initForms();
    loadData();
    
    // Set theme colors from Telegram
    if (tg.themeParams) {
        document.documentElement.style.setProperty('--tg-theme-bg-color', tg.themeParams.bg_color || '#ffffff');
        document.documentElement.style.setProperty('--tg-theme-text-color', tg.themeParams.text_color || '#000000');
        document.documentElement.style.setProperty('--tg-theme-hint-color', tg.themeParams.hint_color || '#999999');
        document.documentElement.style.setProperty('--tg-theme-button-color', tg.themeParams.button_color || '#2481cc');
        document.documentElement.style.setProperty('--tg-theme-button-text-color', tg.themeParams.button_text_color || '#ffffff');
    }
});

// Navigation
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const page = item.dataset.page;
            switchPage(page);
        });
    });
    
    // Filter tabs
    const filterTabs = document.querySelectorAll('.filter-tab');
    filterTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            filterTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentFilter = tab.dataset.filter;
            loadTasks();
        });
    });
}

function switchPage(page) {
    // Update nav
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.page === page);
    });
    
    // Update pages
    document.querySelectorAll('.page').forEach(p => {
        p.classList.toggle('active', p.id === `${page}-page`);
    });
    
    currentPage = page;
    
    // Load data for the page
    switch(page) {
        case 'tasks':
            loadTasks();
            break;
        case 'expenses':
            loadExpenses();
            break;
        case 'reminders':
            loadReminders();
            break;
        case 'summary':
            loadSummary();
            break;
    }
}

// Modals
function initModals() {
    // Task modal
    const taskModal = document.getElementById('taskModal');
    const createTaskBtn = document.getElementById('createTaskBtn');
    const closeTaskModal = document.getElementById('closeTaskModal');
    const cancelTaskBtn = document.getElementById('cancelTaskBtn');
    
    createTaskBtn?.addEventListener('click', () => openModal('taskModal'));
    closeTaskModal?.addEventListener('click', () => closeModal('taskModal'));
    cancelTaskBtn?.addEventListener('click', () => closeModal('taskModal'));
    
    // Expense modal
    const expenseModal = document.getElementById('expenseModal');
    const addExpenseBtn = document.getElementById('addExpenseBtn');
    const closeExpenseModal = document.getElementById('closeExpenseModal');
    const cancelExpenseBtn = document.getElementById('cancelExpenseBtn');
    
    addExpenseBtn?.addEventListener('click', () => openModal('expenseModal'));
    closeExpenseModal?.addEventListener('click', () => closeModal('expenseModal'));
    cancelExpenseBtn?.addEventListener('click', () => closeModal('expenseModal'));
    
    // Reminder modal
    const reminderModal = document.getElementById('reminderModal');
    const createReminderBtn = document.getElementById('createReminderBtn');
    const closeReminderModal = document.getElementById('closeReminderModal');
    const cancelReminderBtn = document.getElementById('cancelReminderBtn');
    
    createReminderBtn?.addEventListener('click', () => openModal('reminderModal'));
    closeReminderModal?.addEventListener('click', () => closeModal('reminderModal'));
    cancelReminderBtn?.addEventListener('click', () => closeModal('reminderModal'));
    
    // Close on backdrop click
    [taskModal, expenseModal, reminderModal].forEach(modal => {
        modal?.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeModal(modal.id);
            }
        });
    });
}

function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('active');
        tg.BackButton.show();
        tg.BackButton.onClick(() => closeModal(modalId));
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('active');
        tg.BackButton.hide();
        tg.BackButton.offClick(() => closeModal(modalId));
    }
}

// Forms
function initForms() {
    // Task form
    const saveTaskBtn = document.getElementById('saveTaskBtn');
    saveTaskBtn?.addEventListener('click', async () => {
        const text = document.getElementById('taskText').value;
        const assignee = document.getElementById('taskAssignee').value;
        const deadline = document.getElementById('taskDeadline').value;
        const recurrence = document.getElementById('taskRecurrence').value;
        
        if (!text) {
            tg.showAlert('Введите описание задачи');
            return;
        }
        
        try {
            await createTask(text, assignee, deadline, recurrence);
            closeModal('taskModal');
            document.getElementById('taskText').value = '';
            document.getElementById('taskAssignee').value = '';
            document.getElementById('taskDeadline').value = '';
            document.getElementById('taskRecurrence').value = 'none';
            loadTasks();
            tg.showPopup({
                title: 'Успешно',
                message: 'Задача создана!',
                buttons: [{ type: 'ok' }]
            });
        } catch (error) {
            tg.showAlert('Ошибка при создании задачи: ' + error.message);
        }
    });
    
    // Expense form
    const saveExpenseBtn = document.getElementById('saveExpenseBtn');
    saveExpenseBtn?.addEventListener('click', async () => {
        const amount = parseFloat(document.getElementById('expenseAmount').value);
        const description = document.getElementById('expenseDescription').value;
        
        if (!amount || amount <= 0) {
            tg.showAlert('Введите корректную сумму');
            return;
        }
        
        if (!description) {
            tg.showAlert('Введите описание расхода');
            return;
        }
        
        try {
            await createExpense(amount, description);
            closeModal('expenseModal');
            document.getElementById('expenseAmount').value = '';
            document.getElementById('expenseDescription').value = '';
            loadExpenses();
            tg.showPopup({
                title: 'Успешно',
                message: 'Расход добавлен!',
                buttons: [{ type: 'ok' }]
            });
        } catch (error) {
            tg.showAlert('Ошибка при добавлении расхода: ' + error.message);
        }
    });
    
    // Reminder form
    const saveReminderBtn = document.getElementById('saveReminderBtn');
    saveReminderBtn?.addEventListener('click', async () => {
        const text = document.getElementById('reminderText').value;
        const time = document.getElementById('reminderTime').value;
        
        if (!text) {
            tg.showAlert('Введите текст напоминания');
            return;
        }
        
        if (!time) {
            tg.showAlert('Укажите время напоминания');
            return;
        }
        
        try {
            await createReminder(text, time);
            closeModal('reminderModal');
            document.getElementById('reminderText').value = '';
            document.getElementById('reminderTime').value = '';
            loadReminders();
            tg.showPopup({
                title: 'Успешно',
                message: 'Напоминание создано!',
                buttons: [{ type: 'ok' }]
            });
        } catch (error) {
            tg.showAlert('Ошибка при создании напоминания: ' + error.message);
        }
    });
}

// API Calls
async function apiCall(endpoint, method = 'GET', body = null) {
    const options = {
        method,
        headers: {
            'Content-Type': 'application/json',
        }
    };
    
    if (body) {
        options.body = JSON.stringify(body);
    }
    
    // Add Telegram init data for authentication
    if (tg.initData) {
        options.headers['X-Telegram-Init-Data'] = tg.initData;
    }
    
    const response = await fetch(`${API_BASE}${endpoint}`, options);
    
    if (!response.ok) {
        const error = await response.json().catch(() => ({ message: 'Unknown error' }));
        throw new Error(error.message || `HTTP ${response.status}`);
    }
    
    return response.json();
}

// Load Data
async function loadData() {
    loadTasks();
}

async function loadTasks() {
    const list = document.getElementById('tasksList');
    if (!list) return;
    
    list.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const tasks = await apiCall('/tasks');
        renderTasks(tasks);
    } catch (error) {
        list.innerHTML = `<div class="empty-state">Ошибка загрузки: ${error.message}</div>`;
    }
}

async function loadExpenses() {
    const list = document.getElementById('expensesList');
    if (!list) return;
    
    list.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const data = await apiCall('/expenses');
        renderExpenses(data.expenses || []);
        
        // Update stats
        if (data.stats) {
            document.getElementById('todayExpenses').textContent = 
                formatCurrency(data.stats.today || 0);
            document.getElementById('monthExpenses').textContent = 
                formatCurrency(data.stats.month || 0);
        }
    } catch (error) {
        list.innerHTML = `<div class="empty-state">Ошибка загрузки: ${error.message}</div>`;
    }
}

async function loadReminders() {
    const list = document.getElementById('remindersList');
    if (!list) return;
    
    list.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const reminders = await apiCall('/reminders');
        renderReminders(reminders);
    } catch (error) {
        list.innerHTML = `<div class="empty-state">Ошибка загрузки: ${error.message}</div>`;
    }
}

async function loadSummary() {
    const content = document.getElementById('summaryContent');
    if (!content) return;
    
    content.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const summary = await apiCall('/summary');
        renderSummary(summary);
    } catch (error) {
        content.innerHTML = `<div class="empty-state">Ошибка загрузки: ${error.message}</div>`;
    }
}

// Render Functions
function renderTasks(tasks) {
    const list = document.getElementById('tasksList');
    if (!list) return;
    
    if (!tasks || tasks.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">📋</div>
                <div>Нет задач</div>
            </div>
        `;
        return;
    }
    
    // Filter tasks
    let filtered = tasks;
    if (currentFilter === 'open') {
        filtered = tasks.filter(t => t.status === 'open');
    } else if (currentFilter === 'overdue') {
        const now = new Date();
        filtered = tasks.filter(t => 
            t.status === 'open' && new Date(t.deadline) < now
        );
    }
    
    list.innerHTML = filtered.map(task => `
        <div class="task-card ${isOverdue(task) ? 'overdue' : ''}">
            <div class="task-header">
                <div class="task-text">${escapeHtml(task.text)}</div>
                <span class="task-status ${task.status}">${getStatusLabel(task.status)}</span>
            </div>
            <div class="task-meta">
                ${task.assignee ? `
                    <div class="task-assignee">
                        <span>👤</span>
                        <span>${escapeHtml(task.assignee)}</span>
                    </div>
                ` : ''}
                <div class="task-deadline">
                    <span>📅</span>
                    <span>${formatDate(task.deadline)}</span>
                </div>
            </div>
            <div class="task-actions">
                <button class="btn btn-primary" onclick="closeTask(${task.id})">
                    ✅ Закрыть
                </button>
            </div>
        </div>
    `).join('');
}

function renderExpenses(expenses) {
    const list = document.getElementById('expensesList');
    if (!list) return;
    
    if (!expenses || expenses.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">💰</div>
                <div>Нет расходов</div>
            </div>
        `;
        return;
    }
    
    list.innerHTML = expenses.map(expense => `
        <div class="expense-card">
            <div class="expense-info">
                <div class="expense-amount">${formatCurrency(expense.amount)}</div>
                <div class="expense-description">${escapeHtml(expense.description)}</div>
                ${expense.category ? `
                    <div class="expense-category">${escapeHtml(expense.category)}</div>
                ` : ''}
            </div>
        </div>
    `).join('');
}

function renderReminders(reminders) {
    const list = document.getElementById('remindersList');
    if (!list) return;
    
    if (!reminders || reminders.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">⏰</div>
                <div>Нет напоминаний</div>
            </div>
        `;
        return;
    }
    
    list.innerHTML = reminders.map(reminder => `
        <div class="reminder-card">
            <div class="reminder-time">${formatDate(reminder.remind_at, true)}</div>
            <div class="reminder-text">${escapeHtml(reminder.text)}</div>
        </div>
    `).join('');
}

function renderSummary(summary) {
    const content = document.getElementById('summaryContent');
    if (!content) return;
    
    if (!summary || !summary.text) {
        content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">📊</div>
                <div>Нет саммари</div>
            </div>
        `;
        return;
    }
    
    content.innerHTML = `
        <div class="summary-content">
            <div class="summary-section">
                ${summary.text.split('\n\n').map(section => {
                    if (section.startsWith('**')) {
                        const title = section.match(/\*\*(.*?)\*\*/)?.[1] || '';
                        const text = section.replace(/\*\*.*?\*\*/, '').trim();
                        return `
                            <div class="summary-section">
                                <h3>${escapeHtml(title)}</h3>
                                <p>${escapeHtml(text)}</p>
                            </div>
                        `;
                    }
                    return `<p>${escapeHtml(section)}</p>`;
                }).join('')}
            </div>
        </div>
    `;
}

// Create Functions
async function createTask(text, assignee, deadline, recurrence) {
    return apiCall('/tasks', 'POST', {
        text,
        assignee,
        deadline,
        recurrence
    });
}

async function createExpense(amount, description) {
    return apiCall('/expenses', 'POST', {
        amount,
        description
    });
}

async function createReminder(text, time) {
    return apiCall('/reminders', 'POST', {
        text,
        time
    });
}

async function closeTask(taskId) {
    try {
        await apiCall(`/tasks/${taskId}/close`, 'POST');
        loadTasks();
        tg.showPopup({
            title: 'Успешно',
            message: 'Задача закрыта!',
            buttons: [{ type: 'ok' }]
        });
    } catch (error) {
        tg.showAlert('Ошибка при закрытии задачи: ' + error.message);
    }
}

// Utility Functions
function formatDate(dateString, includeTime = false) {
    const date = new Date(dateString);
    const now = new Date();
    const diff = date - now;
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));
    
    if (days === 0) {
        return includeTime ? date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : 'Сегодня';
    } else if (days === 1) {
        return 'Завтра';
    } else if (days === -1) {
        return 'Вчера';
    } else if (days < 0) {
        return `Просрочено на ${Math.abs(days)} дн.`;
    } else {
        return date.toLocaleDateString('ru-RU', { 
            day: 'numeric', 
            month: 'short',
            ...(includeTime && { hour: '2-digit', minute: '2-digit' })
        });
    }
}

function formatCurrency(amount) {
    return new Intl.NumberFormat('ru-RU', {
        style: 'currency',
        currency: 'RUB',
        minimumFractionDigits: 0
    }).format(amount);
}

function getStatusLabel(status) {
    const labels = {
        open: 'Активна',
        closed: 'Закрыта'
    };
    return labels[status] || status;
}

function isOverdue(task) {
    if (task.status !== 'open') return false;
    return new Date(task.deadline) < new Date();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Make functions available globally
window.closeTask = closeTask;

