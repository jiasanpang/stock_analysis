import type React from 'react';
import { useEffect } from 'react';
import {BrowserRouter as Router, Routes, Route, NavLink, useLocation, Navigate} from 'react-router-dom';
import HomePage from './pages/HomePage';
import BacktestPage from './pages/BacktestPage';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';
import NotFoundPage from './pages/NotFoundPage';
import ChatPage from './pages/ChatPage';
import PickerPage from './pages/PickerPage';
import { ApiErrorAlert, Spinner } from './components/common';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { useAgentChatStore } from './stores/agentChatStore';
import './App.css';

// 侧边导航图标
const HomeIcon: React.FC<{ active?: boolean }> = ({active}) => (
    <svg className="w-6 h-6" fill={active ? 'currentColor' : 'none'} stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/>
    </svg>
);

const BacktestIcon: React.FC<{ active?: boolean }> = ({active}) => (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={active ? 2 : 1.5}
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
    </svg>
);

const SettingsIcon: React.FC<{ active?: boolean }> = ({active}) => (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={active ? 2 : 1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
    </svg>
);

const ChatIcon: React.FC<{ active?: boolean }> = ({active}) => (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={active ? 2 : 1.5}
              d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/>
    </svg>
);

const PickerIcon: React.FC<{ active?: boolean }> = ({active}) => (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={active ? 2 : 1.5}
              d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
    </svg>
);

const LogoutIcon: React.FC = () => (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
    </svg>
);

type DockItem = {
    key: string;
    label: string;
    to: string;
    icon: React.FC<{ active?: boolean }>;
};

const NAV_ITEMS: DockItem[] = [
    {
        key: 'home',
        label: '首页',
        to: '/',
        icon: HomeIcon,
    },
    {
        key: 'chat',
        label: '问股',
        to: '/chat',
        icon: ChatIcon,
    },
    {
        key: 'picker',
        label: '选股',
        to: '/picker',
        icon: PickerIcon,
    },
    {
        key: 'backtest',
        label: '回测',
        to: '/backtest',
        icon: BacktestIcon,
    },
];

// Top Navigation Bar
const TopNav: React.FC = () => {
    const {authEnabled, logout} = useAuth();
    const completionBadge = useAgentChatStore((s) => s.completionBadge);
    return (
        <header className="fixed top-0 left-0 right-0 z-50 h-12 bg-white border-b border-gray-200">
            <div className="h-full px-4 flex items-center justify-between">
                {/* Left: Logo + Brand + Nav */}
                <div className="flex items-center gap-6">
                    <NavLink to="/" className="flex items-center gap-2.5" title="首页">
                        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan to-purple flex items-center justify-center">
                            <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5}
                                      d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>
                            </svg>
                        </div>
                        <div className="hidden sm:block">
                            <div className="font-bold text-sm text-gray-900 tracking-wide">STOCK ANALYSIS</div>
                            <div className="text-[10px] text-gray-500 -mt-0.5">AI 智能分析平台</div>
                        </div>
                    </NavLink>

                    <nav className="flex items-center gap-0.5" aria-label="主导航">
                        {NAV_ITEMS.map((item) => {
                            const Icon = item.icon;
                            if (item.key === 'chat') {
                                return (
                                    <div key="chat" className="relative">
                                        <NavLink
                                            to="/chat"
                                            end={false}
                                            className={({isActive}) => 
                                                `flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition ${
                                                    isActive 
                                                        ? 'bg-gray-100 text-gray-900' 
                                                        : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'
                                                }`
                                            }
                                        >
                                            {({isActive}) => (
                                                <>
                                                    <Icon active={isActive}/>
                                                    <span>{item.label}</span>
                                                </>
                                            )}
                                        </NavLink>
                                        {completionBadge && (
                                            <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-cyan"/>
                                        )}
                                    </div>
                                );
                            }
                            return (
                                <NavLink
                                    key={item.key}
                                    to={item.to}
                                    end={item.to === '/'}
                                    className={({isActive}) => 
                                        `flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition ${
                                            isActive 
                                                ? 'bg-gray-100 text-gray-900' 
                                                : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'
                                        }`
                                    }
                                >
                                    {({isActive}) => (
                                        <>
                                            <Icon active={isActive}/>
                                            <span>{item.label}</span>
                                        </>
                                    )}
                                </NavLink>
                            );
                        })}
                    </nav>
                </div>

                {/* Right: Settings + Logout */}
                <div className="flex items-center gap-1">
                    <NavLink
                        to="/settings"
                        className={({isActive}) => 
                            `p-2 rounded-md transition ${
                                isActive 
                                    ? 'bg-gray-100 text-gray-900' 
                                    : 'text-gray-500 hover:text-gray-700 hover:bg-gray-50'
                            }`
                        }
                        title="设置"
                    >
                        <SettingsIcon/>
                    </NavLink>
                    {authEnabled && (
                        <button
                            type="button"
                            onClick={() => logout()}
                            title="退出登录"
                            className="p-2 rounded-md text-gray-500 hover:text-red-600 hover:bg-red-50 transition"
                        >
                            <LogoutIcon/>
                        </button>
                    )}
                </div>
            </div>
        </header>
    );
};

const AppContent: React.FC = () => {
    const location = useLocation();
    const { authEnabled, loggedIn, isLoading, loadError, refreshStatus } = useAuth();

    useEffect(() => {
        useAgentChatStore.getState().setCurrentRoute(location.pathname);
    }, [location.pathname]);

    if (isLoading) {
        return (
            <div className="flex min-h-screen items-center justify-center bg-base">
                <Spinner size="lg" />
            </div>
        );
    }

    if (loadError) {
        return (
            <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4">
                <div className="w-full max-w-lg">
                    <ApiErrorAlert error={loadError}/>
                </div>
                <button
                    type="button"
                    className="btn-primary"
                    onClick={() => void refreshStatus()}
                >
                    重试
                </button>
            </div>
        );
    }

    if (authEnabled && !loggedIn) {
        if (location.pathname === '/login') {
            return <LoginPage />;
        }
        const redirect = encodeURIComponent(location.pathname + location.search);
        return <Navigate to={`/login?redirect=${redirect}`} replace />;
    }

    if (location.pathname === '/login') {
        return <Navigate to="/" replace />;
    }

    return (
        <div className="flex flex-col min-h-screen bg-base">
            <TopNav/>
            <main className="flex-1 pt-12">
                <Routes>
                    <Route path="/" element={<HomePage/>}/>
                    <Route path="/chat" element={<ChatPage/>}/>
                    <Route path="/picker" element={<PickerPage/>}/>
                    <Route path="/backtest" element={<BacktestPage/>}/>
                    <Route path="/settings" element={<SettingsPage/>}/>
                    <Route path="/login" element={<LoginPage/>}/>
                    <Route path="*" element={<NotFoundPage/>}/>
                </Routes>
            </main>
        </div>
    );
};

const App: React.FC = () => {
    return (
        <Router>
            <AuthProvider>
                <AppContent/>
            </AuthProvider>
        </Router>
    );
};

export default App;
