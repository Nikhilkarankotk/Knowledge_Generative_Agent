import { Component } from '@angular/core';
import { Sidebar } from './components/sidebar/sidebar';
import { ChatInterface } from './components/chat-interface/chat-interface';

@Component({
  selector: 'app-root',
  imports: [Sidebar, ChatInterface],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App {
}
